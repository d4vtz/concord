import base64
import json
import os
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from concord import application as concord
from concord.application.config import (CONCORD_VERSION, ConfigManager,
                                        ManifestReference, SecretConfig,
                                        SecretGroupConfig)
from concord.application.database import Database

PARTIAL_SUFFIX = ".concord-secrets.age"


@dataclass(frozen=True)
class Secret:
    id: str
    target_id: str
    target_name: str
    target_path_id: str
    relative_file: Path
    kind: str
    mode: int
    names: list[str]


class Age:
    @staticmethod
    def available() -> bool:
        return shutil.which("age") is not None and shutil.which("age-keygen") is not None

    @staticmethod
    def _run(arguments: list[str], data: bytes, passphrase: str | None = None) -> bytes:
        environment = os.environ.copy()
        if passphrase is not None:
            environment["AGE_PASSPHRASE"] = passphrase
        process = subprocess.run(
            arguments, input=data, capture_output=True, env=environment, check=False
        )
        if process.returncode:
            raise ValueError(process.stderr.decode(errors="replace").strip() or "age falló.")
        return process.stdout

    @classmethod
    def generate_identity(cls) -> tuple[str, str]:
        process = subprocess.run(["age-keygen"], capture_output=True, text=True, check=False)
        if process.returncode:
            raise ValueError(process.stderr.strip() or "No se pudo generar la identidad age.")
        identity = next((line for line in process.stdout.splitlines() if line.startswith("AGE-SECRET-KEY-")), "")
        if not identity:
            raise ValueError("age-keygen no devolvió una identidad válida.")
        recipient = cls._run(["age-keygen", "-y"], (identity + "\n").encode()).decode().strip()
        return identity, recipient

    @classmethod
    def encrypt_passphrase(cls, data: bytes, password: str) -> bytes:
        return cls._run(["age", "--passphrase"], data, password)

    @classmethod
    def decrypt_passphrase(cls, data: bytes, password: str) -> bytes:
        return cls._run(["age", "--decrypt"], data, password)

    @classmethod
    def encrypt(cls, data: bytes, recipient: str) -> bytes:
        return cls._run(["age", "--recipient", recipient], data)

    @classmethod
    def decrypt(cls, data: bytes, identity: str) -> bytes:
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, identity.encode() + b"\n")
            os.close(write_fd)
            write_fd = -1
            process = subprocess.run(
                ["age", "--decrypt", "--identity", f"/proc/self/fd/{read_fd}"],
                input=data,
                capture_output=True,
                pass_fds=(read_fd,),
                check=False,
            )
            if process.returncode:
                raise ValueError(process.stderr.decode(errors="replace").strip() or "age falló.")
            return process.stdout
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)


class SecretManager:
    def __init__(self, database: Database | None = None, config_manager: ConfigManager | None = None):
        self.database = database or Database()
        self.config_manager = config_manager or ConfigManager()
        self.database.initialize()
        self._identity: str | None = None

    def configured(self) -> bool:
        with self.database.connect() as connection:
            return connection.execute("SELECT 1 FROM repository_secret_group WHERE singleton=1").fetchone() is not None

    def initialize(self, master: str, recovery: str, recovery_path: Path) -> str:
        if self.configured():
            raise ValueError("Los secretos ya están configurados para este repositorio.")
        if not Age.available():
            raise FileNotFoundError("age no está instalado.")
        identity, recipient = Age.generate_identity()
        master_wrapper = base64.b64encode(Age.encrypt_passphrase(identity.encode(), master)).decode()
        recovery_wrapper = base64.b64encode(Age.encrypt_passphrase(identity.encode(), recovery)).decode()
        group_id = str(uuid.uuid4())
        recovery_path = recovery_path.expanduser().resolve()
        recovery_path.parent.mkdir(parents=True, exist_ok=True)
        recovery_path.write_text(recovery_wrapper + "\n")
        recovery_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        with self.database.connect() as connection:
            connection.execute(
                "INSERT INTO secret_groups VALUES (?, ?, ?, ?, ?)",
                (group_id, recipient, master_wrapper, recovery_wrapper, str(recovery_path)),
            )
            connection.execute("INSERT INTO repository_secret_group VALUES (1, ?)", (group_id,))
        self._identity = identity
        self.persist_manifest()
        return group_id

    def _group(self):
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT g.id,g.recipient,g.master_wrapper,g.recovery_wrapper,g.recovery_backup_path
                   FROM secret_groups g JOIN repository_secret_group r ON r.group_id=g.id
                   WHERE r.singleton=1"""
            ).fetchone()
        if row is None:
            raise ValueError("Los secretos no están configurados; ejecuta concord secret init.")
        return row

    def unlock(self, password: str, *, recovery: bool = False) -> None:
        row = self._group()
        wrapper = row[3] if recovery else row[2]
        identity = Age.decrypt_passphrase(base64.b64decode(wrapper), password).decode()
        if not identity.startswith("AGE-SECRET-KEY-"):
            raise ValueError("La contraseña no desbloqueó una identidad válida.")
        self._identity = identity.strip()

    def rekey(self, password: str, *, recovery: bool = False) -> None:
        identity = self._require_identity().encode()
        wrapper = base64.b64encode(Age.encrypt_passphrase(identity, password)).decode()
        column = "recovery_wrapper" if recovery else "master_wrapper"
        group = self._group()
        with self.database.connect() as connection:
            connection.execute(f"UPDATE secret_groups SET {column}=? WHERE id=?", (wrapper, group[0]))
        if recovery and group[4]:
            backup = Path(group[4])
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_text(wrapper + "\n")
            backup.chmod(stat.S_IRUSR | stat.S_IWUSR)
        self.persist_manifest()

    def recover_from_backup(self, backup: Path, password: str) -> None:
        wrapper = backup.expanduser().resolve().read_text().strip()
        try:
            identity = Age.decrypt_passphrase(base64.b64decode(wrapper), password).decode().strip()
        except Exception as error:
            raise ValueError("La copia o la contraseña de recuperación no son válidas.") from error
        if not identity.startswith("AGE-SECRET-KEY-"):
            raise ValueError("La copia no contiene una identidad age válida.")
        group = self._group()
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE secret_groups SET recovery_wrapper=?,recovery_backup_path=? WHERE id=?",
                (wrapper, str(backup.expanduser().resolve()), group[0]),
            )
        self._identity = identity
        self.persist_manifest()

    @property
    def recipient(self) -> str:
        return self._group()[1]

    def _require_identity(self) -> str:
        if self._identity is None:
            raise PermissionError("Los secretos están bloqueados.")
        return self._identity

    @property
    def unlocked(self) -> bool:
        return self._identity is not None

    def list(self, target_ids: set[str] | None = None) -> list[Secret]:
        with self.database.connect() as connection:
            rows = connection.execute(
                """SELECT s.id,s.target_id,t.name,s.target_path_id,s.relative_file,s.kind,s.mode
                   FROM secrets s JOIN targets t ON t.id=s.target_id ORDER BY t.name,s.relative_file"""
            ).fetchall()
            result = []
            for row in rows:
                if target_ids is not None and row[1] not in target_ids:
                    continue
                names = [item[0] for item in connection.execute(
                    "SELECT name FROM secret_values WHERE secret_id=? ORDER BY name", (row[0],)
                )]
                result.append(Secret(*row[:4], Path(row[4]), *row[5:], names))
        return result

    def for_target(self, target_id: str) -> list[Secret]:
        return self.list({target_id})

    def locate(self, path: Path, targets) -> tuple[object, object, Path]:
        resolved = path.expanduser().resolve()
        if not resolved.is_file():
            raise ValueError("Solo pueden protegerse archivos individuales.")
        for target in targets:
            for target_path in target.paths:
                root = target_path.local_path
                if target_path.type.value == "file" and resolved == root:
                    return target, target_path, Path(".")
                if target_path.type.value == "directory" and resolved.is_relative_to(root):
                    return target, target_path, resolved.relative_to(root)
        raise ValueError("El archivo no pertenece a ningún target registrado.")

    def protect(self, path: Path, targets) -> Secret:
        target, target_path, relative = self.locate(path, targets)
        if target.name == "concord":
            raise ValueError("El target interno concord no puede contener secretos.")
        mode = stat.S_IMODE(path.expanduser().resolve().stat().st_mode)
        secret_id = str(uuid.uuid4())
        with self.database.connect() as connection:
            existing = connection.execute(
                "SELECT id,kind FROM secrets WHERE target_path_id=? AND relative_file=?",
                (target_path.id, relative.as_posix()),
            ).fetchone()
            if existing and existing[1] != "excluded":
                raise ValueError("El archivo ya está protegido.")
            if existing:
                secret_id = existing[0]
                connection.execute("UPDATE secrets SET kind='file',mode=? WHERE id=?", (mode, secret_id))
            else:
                connection.execute(
                    "INSERT INTO secrets VALUES (?, ?, ?, ?, 'file', ?)",
                    (secret_id, target.id, target_path.id, relative.as_posix(), mode),
                )
        self.persist_manifest()
        return self.for_target(target.id)[-1]

    def set_value(self, path: Path, name: str, value: str, targets) -> Secret:
        if not name or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in name):
            raise ValueError("El nombre solo puede contener letras, números, '-' y '_'.")
        target, target_path, relative = self.locate(path, targets)
        if target.name == "concord":
            raise ValueError("El target interno concord no puede contener secretos.")
        local = path.expanduser().resolve()
        try:
            text = local.read_text()
        except UnicodeDecodeError as error:
            raise ValueError("Los secretos parciales solo admiten archivos de texto.") from error
        if value not in text:
            raise ValueError("El valor indicado no aparece en el archivo.")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id,kind FROM secrets WHERE target_path_id=? AND relative_file=?",
                (target_path.id, relative.as_posix()),
            ).fetchone()
            if row and row[1] != "partial":
                raise ValueError("El archivo ya está protegido completamente.")
            secret_id = row[0] if row else str(uuid.uuid4())
            if not row:
                connection.execute(
                    "INSERT INTO secrets VALUES (?, ?, ?, ?, 'partial', ?)",
                    (secret_id, target.id, target_path.id, relative.as_posix(), stat.S_IMODE(local.stat().st_mode)),
                )
            ciphertext = base64.b64encode(Age.encrypt(value.encode(), self.recipient)).decode()
            connection.execute(
                "INSERT OR REPLACE INTO secret_values VALUES (?, ?, ?)",
                (secret_id, name.lower(), ciphertext),
            )
        self.persist_manifest()
        return next(item for item in self.for_target(target.id) if item.id == secret_id)

    def _stored_path(self, root: Path, secret: Secret) -> Path:
        return root if secret.relative_file == Path(".") else root / secret.relative_file

    def path_clean(self, target, target_path, stored: Path) -> bool:
        identity = self._require_identity()
        secrets = [item for item in self.for_target(target.id) if item.target_path_id == target_path.id]
        if target_path.type.value == "file":
            secret = next((item for item in secrets if item.relative_file == Path(".")), None)
            if secret is None:
                return stored.is_file() and target_path.local_path.read_bytes() == stored.read_bytes()
            if secret.kind == "excluded":
                return not stored.exists() and not stored.with_name(stored.name + ".age").exists()
            if secret.kind == "file":
                encrypted = stored.with_name(stored.name + ".age")
                return encrypted.is_file() and target_path.local_path.read_bytes() == Age.decrypt(encrypted.read_bytes(), identity)
            bundle = stored.with_name(stored.name + PARTIAL_SUFFIX)
            if not stored.is_file() or not bundle.is_file():
                return False
            values = json.loads(Age.decrypt(bundle.read_bytes(), identity))
            text = stored.read_text()
            for name, value in values.items():
                text = text.replace("{{ concord_secret: " + name + " }}", value)
            return target_path.local_path.read_text() == text
        if not stored.is_dir():
            return False
        secret_by_path = {item.relative_file: item for item in secrets}
        local_files = {item.relative_to(target_path.local_path): item for item in target_path.local_path.rglob("*") if item.is_file()}
        represented = set()
        for relative, local in local_files.items():
            secret = secret_by_path.get(relative)
            remote = stored / relative
            if secret and secret.kind == "excluded":
                if remote.exists() or remote.with_name(remote.name + ".age").exists():
                    return False
            elif secret and secret.kind == "file":
                encrypted = remote.with_name(remote.name + ".age")
                if not encrypted.is_file() or local.read_bytes() != Age.decrypt(encrypted.read_bytes(), identity):
                    return False
                represented.add(relative.with_name(relative.name + ".age"))
            elif secret and secret.kind == "partial":
                bundle = remote.with_name(remote.name + PARTIAL_SUFFIX)
                if not remote.is_file() or not bundle.is_file():
                    return False
                values = json.loads(Age.decrypt(bundle.read_bytes(), identity))
                text = remote.read_text()
                for name, value in values.items():
                    text = text.replace("{{ concord_secret: " + name + " }}", value)
                if local.read_text() != text:
                    return False
                represented.update({relative, relative.with_name(relative.name + PARTIAL_SUFFIX)})
            else:
                if not remote.is_file() or local.read_bytes() != remote.read_bytes():
                    return False
                represented.add(relative)
        remote_files = {item.relative_to(stored) for item in stored.rglob("*") if item.is_file()}
        return remote_files == represented

    def protect_stage(self, target, temporary: Path) -> None:
        identity = self._require_identity()
        for secret in self.for_target(target.id):
            target_path = next(path for path in target.paths if path.id == secret.target_path_id)
            base = temporary / target_path.relative_path
            source = self._stored_path(base, secret)
            if secret.kind == "excluded":
                if source.exists() or source.is_symlink():
                    source.unlink()
                continue
            if secret.kind == "file":
                encrypted = Age.encrypt(source.read_bytes(), self.recipient)
                destination = source.with_name(source.name + ".age")
                destination.write_bytes(encrypted)
                source.unlink()
                continue
            text = source.read_text()
            values = {}
            with self.database.connect() as connection:
                rows = connection.execute(
                    "SELECT name,value_ciphertext FROM secret_values WHERE secret_id=? ORDER BY name",
                    (secret.id,),
                ).fetchall()
            for name, ciphertext in rows:
                value = Age.decrypt(base64.b64decode(ciphertext), identity).decode()
                if value not in text:
                    raise ValueError(f"No se encontró el valor del secreto '{name}' en {source}.")
                text = text.replace(value, "{{ concord_secret: " + name + " }}")
                values[name] = value
            source.write_text(text)
            bundle = Age.encrypt(json.dumps(values, ensure_ascii=False).encode(), self.recipient)
            source.with_name(source.name + PARTIAL_SUFFIX).write_bytes(bundle)

    def restore_stage(self, target, temporary_root: Path) -> None:
        identity = self._require_identity()
        for secret in self.for_target(target.id):
            target_path = next(path for path in target.paths if path.id == secret.target_path_id)
            base = temporary_root / target_path.relative_path
            destination = self._stored_path(base, secret)
            if secret.kind == "file":
                encrypted = destination.with_name(destination.name + ".age")
                destination.write_bytes(Age.decrypt(encrypted.read_bytes(), identity))
                destination.chmod(secret.mode)
                encrypted.unlink()
                continue
            bundle_path = destination.with_name(destination.name + PARTIAL_SUFFIX)
            values = json.loads(Age.decrypt(bundle_path.read_bytes(), identity))
            text = destination.read_text()
            for name, value in values.items():
                marker = "{{ concord_secret: " + name + " }}"
                if marker not in text:
                    raise ValueError(f"Falta el marcador '{name}' en {destination}.")
                text = text.replace(marker, value)
                ciphertext = base64.b64encode(Age.encrypt(value.encode(), self.recipient)).decode()
                with self.database.connect() as connection:
                    connection.execute(
                        "INSERT OR REPLACE INTO secret_values VALUES (?, ?, ?)",
                        (secret.id, name, ciphertext),
                    )
            destination.write_text(text)
            destination.chmod(secret.mode)
            bundle_path.unlink()

    def stage_restore_path(self, target, target_path, stored: Path, temporary: Path) -> None:
        identity = self._require_identity()
        secrets = [item for item in self.for_target(target.id) if item.target_path_id == target_path.id and item.kind != "excluded"]
        if target_path.type.value == "file":
            secret = next((item for item in secrets if item.relative_file == Path(".")), None)
            if secret is None:
                shutil.copy2(stored, temporary, follow_symlinks=False)
                return
            if secret.kind == "file":
                encrypted = stored.with_name(stored.name + ".age")
                temporary.write_bytes(Age.decrypt(encrypted.read_bytes(), identity))
                temporary.chmod(secret.mode)
                return
            values = json.loads(Age.decrypt(stored.with_name(stored.name + PARTIAL_SUFFIX).read_bytes(), identity))
            text = stored.read_text()
            for name, value in values.items():
                text = text.replace("{{ concord_secret: " + name + " }}", value)
            temporary.write_text(text)
            temporary.chmod(secret.mode)
            return
        shutil.copytree(stored, temporary, symlinks=True)
        for secret in secrets:
            destination = temporary / secret.relative_file
            if secret.kind == "file":
                encrypted = destination.with_name(destination.name + ".age")
                destination.write_bytes(Age.decrypt(encrypted.read_bytes(), identity))
                destination.chmod(secret.mode)
                encrypted.unlink()
            else:
                bundle_path = destination.with_name(destination.name + PARTIAL_SUFFIX)
                values = json.loads(Age.decrypt(bundle_path.read_bytes(), identity))
                text = destination.read_text()
                for name, value in values.items():
                    marker = "{{ concord_secret: " + name + " }}"
                    if marker not in text:
                        raise ValueError(f"Falta el marcador '{name}' en {destination}.")
                    text = text.replace(marker, value)
                    ciphertext = base64.b64encode(Age.encrypt(value.encode(), self.recipient)).decode()
                    with self.database.connect() as connection:
                        connection.execute(
                            "INSERT OR REPLACE INTO secret_values VALUES (?, ?, ?)",
                            (secret.id, name, ciphertext),
                        )
                destination.write_text(text)
                destination.chmod(secret.mode)
                bundle_path.unlink()

    def unprotect(self, path: Path, targets) -> Secret:
        target, target_path, relative = self.locate(path, targets)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM secrets WHERE target_path_id=? AND relative_file=?",
                (target_path.id, relative.as_posix()),
            ).fetchone()
            if row is None:
                raise KeyError("El archivo no está protegido.")
            secret = next(item for item in self.for_target(target.id) if item.id == row[0])
            connection.execute("UPDATE secrets SET kind='excluded' WHERE id=?", (row[0],))
            connection.execute("DELETE FROM secret_values WHERE secret_id=?", (row[0],))
        self.persist_manifest()
        return secret

    def apply_to_config(self, config) -> None:
        if not self.configured():
            config.secret_group = None
            config.secrets = []
            return
        group = self._group()
        config.secret_group = SecretGroupConfig(group[0], group[1], group[2], group[3])
        config.secrets = [
            SecretConfig(
                item.id, ManifestReference(item.target_id, item.target_name), item.target_path_id,
                item.relative_file, item.kind, item.mode, item.names
            )
            for item in self.list()
        ]
        if config.secrets:
            config.minimum_concord_version = CONCORD_VERSION

    def persist_manifest(self) -> None:
        if not concord.config_file.exists():
            return
        config = self.config_manager.load()
        self.apply_to_config(config)
        self.config_manager.save(config)

    def import_config(self, config, connection=None) -> None:
        own = connection is None
        connection = connection or self.database.connect()
        try:
            connection.execute("DELETE FROM secrets")
            connection.execute("DELETE FROM repository_secret_group")
            if config.secret_group:
                group = config.secret_group
                connection.execute(
                    "INSERT OR REPLACE INTO secret_groups (id,recipient,master_wrapper,recovery_wrapper,recovery_backup_path) VALUES (?,?,?,?,NULL)",
                    (group.id, group.recipient, group.master_wrapper, group.recovery_wrapper),
                )
                connection.execute("INSERT INTO repository_secret_group VALUES (1,?)", (group.id,))
                for item in config.secrets:
                    if item.target.name == "concord":
                        raise ValueError("El target concord no puede contener secretos.")
                    connection.execute(
                        "INSERT INTO secrets VALUES (?,?,?,?,?,?)",
                        (item.id,item.target.id,item.target_path_id,item.relative_file.as_posix(),item.kind,item.mode),
                    )
                    for name in item.names:
                        connection.execute(
                            "INSERT INTO secret_values VALUES (?,?,'')", (item.id,name)
                        )
            if own:
                connection.commit()
        finally:
            if own:
                connection.close()
