import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field, replace

from concord.application.config import (CONCORD_TARGET,
                                        PROFILE_MINIMUM_VERSION, Config,
                                        ConfigManager, ManifestReference,
                                        ProfileConfig,
                                        SuggestedActivationConfig)
from concord.application.database import Database


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    description: str = ""
    includes: list[str] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Activation:
    primary: str
    complements: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ProfileResolution:
    target_ids: list[str]
    target_names: list[str]
    warnings: list[str]
    activation: Activation
    applied_exclusions: list[str] = field(default_factory=list)


class ProfileManager:
    RESERVED_NAMES = {"all", "none", "default"}
    NAME_PATTERN = re.compile(r"^[a-z0-9_-]+$")

    def __init__(
        self,
        database: Database | None = None,
        config_manager: ConfigManager | None = None,
    ) -> None:
        self.database = database or Database()
        self.config_manager = config_manager or ConfigManager()
        self.database.initialize()

    def _normalize_name(self, name: str) -> str:
        normalized = name.strip().lower()
        if not self.NAME_PATTERN.fullmatch(normalized):
            raise ValueError(
                "El nombre del perfil solo puede contener letras, números, guiones y guiones bajos."
            )
        if normalized in self.RESERVED_NAMES:
            raise ValueError(f"'{normalized}' es un nombre reservado por Concord.")
        return normalized

    def _profile_from_row(self, connection, row) -> Profile:
        profile_id = row[0]

        def names(table: str, column: str, joined: str) -> list[str]:
            return [
                item[0]
                for item in connection.execute(
                    f"""
                    SELECT {joined}.name
                    FROM {table}
                    JOIN {joined} ON {joined}.id = {table}.{column}
                    WHERE {table}.profile_id = ?
                    ORDER BY {table}.position
                    """,
                    (profile_id,),
                )
            ]

        return Profile(
            id=profile_id,
            name=row[1],
            description=row[2],
            includes=names("profile_includes", "included_profile_id", "profiles"),
            targets=names("profile_targets", "target_id", "targets"),
            excludes=names("profile_exclusions", "target_id", "targets"),
        )

    def get(self, name: str) -> Profile:
        normalized = name.strip().lower()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT id, name, description FROM profiles WHERE name = ?", (normalized,)
            ).fetchone()
            if row is None:
                raise KeyError(f"No existe un perfil llamado '{normalized}'.")
            return self._profile_from_row(connection, row)

    def list(self) -> list[Profile]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, name, description FROM profiles ORDER BY name"
            ).fetchall()
            return [self._profile_from_row(connection, row) for row in rows]

    def _id(self, connection, table: str, name: str, kind: str) -> str:
        row = connection.execute(
            f"SELECT id FROM {table} WHERE name = ?", (name.strip().lower(),)
        ).fetchone()
        if row is None:
            raise KeyError(f"No existe {kind} llamado '{name}'.")
        return row[0]

    def _replace_ordered(
        self,
        connection,
        table: str,
        profile_id: str,
        column: str,
        values: list[str],
        source_table: str,
        kind: str,
    ) -> None:
        connection.execute(f"DELETE FROM {table} WHERE profile_id = ?", (profile_id,))
        ids = []
        for name in values:
            if source_table == "targets" and name.strip().lower() == CONCORD_TARGET:
                raise ValueError("El target interno 'concord' no puede pertenecer a un perfil.")
            item_id = self._id(connection, source_table, name, kind)
            if item_id not in ids:
                ids.append(item_id)
        connection.executemany(
            f"INSERT INTO {table} (profile_id, {column}, position) VALUES (?, ?, ?)",
            [(profile_id, item_id, position) for position, item_id in enumerate(ids)],
        )

    def _save_manifest(self, connection) -> None:
        config = self.config_manager.load()
        self.apply_to_config(config, connection=connection)
        self.config_manager.save(config)

    def create(
        self,
        name: str,
        *,
        description: str = "",
    ) -> Profile:
        normalized = self._normalize_name(name)
        profile_id = str(uuid.uuid4())
        with self.database.connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO profiles (id, name, description) VALUES (?, ?, ?)",
                    (profile_id, normalized, description.strip()),
                )
            except Exception as error:
                if "UNIQUE" in str(error):
                    raise ValueError(f"El perfil '{normalized}' ya existe.") from error
                raise
            self._save_manifest(connection)
        return self.get(normalized)

    def update(
        self,
        name: str,
        *,
        description: str | None = None,
        includes: list[str] | None = None,
        targets: list[str] | None = None,
        excludes: list[str] | None = None,
    ) -> Profile:
        current = self.get(name)
        with self.database.connect() as connection:
            if description is not None:
                connection.execute(
                    "UPDATE profiles SET description = ? WHERE id = ?",
                    (description.strip(), current.id),
                )
            if includes is not None:
                self._replace_ordered(
                    connection,
                    "profile_includes",
                    current.id,
                    "included_profile_id",
                    includes,
                    "profiles",
                    "un perfil",
                )
            if targets is not None:
                self._replace_ordered(
                    connection,
                    "profile_targets",
                    current.id,
                    "target_id",
                    targets,
                    "targets",
                    "un target",
                )
            if excludes is not None:
                self._replace_ordered(
                    connection,
                    "profile_exclusions",
                    current.id,
                    "target_id",
                    excludes,
                    "targets",
                    "un target",
                )
            self._validate_graph(connection)
            self._save_manifest(connection)
        return self.get(current.name)

    def rename(self, name: str, new_name: str) -> Profile:
        current = self.get(name)
        normalized = self._normalize_name(new_name)
        with self.database.connect() as connection:
            try:
                connection.execute(
                    "UPDATE profiles SET name = ? WHERE id = ?", (normalized, current.id)
                )
            except Exception as error:
                if "UNIQUE" in str(error):
                    raise ValueError(f"El perfil '{normalized}' ya existe.") from error
                raise
            self._save_manifest(connection)
        return self.get(normalized)

    def delete(self, name: str) -> None:
        profile = self.get(name)
        active = self.activation()
        if active and profile.name in {active.primary, *active.complements}:
            raise ValueError(
                f"El perfil '{profile.name}' está activo; desactívelo antes de eliminarlo."
            )
        with self.database.connect() as connection:
            suggestion = connection.execute(
                "SELECT primary_profile_id FROM profile_suggestion WHERE singleton = 1"
            ).fetchone()
            if suggestion and suggestion[0] == profile.id:
                connection.execute("DELETE FROM profile_suggestion_complements")
                connection.execute("DELETE FROM profile_suggestion")
            else:
                connection.execute(
                    "DELETE FROM profile_suggestion_complements WHERE profile_id = ?",
                    (profile.id,),
                )
            connection.execute("DELETE FROM profiles WHERE id = ?", (profile.id,))
            self._save_manifest(connection)

    def _validate_graph(self, connection) -> None:
        graph: dict[str, list[str]] = {}
        for profile_id, included_id in connection.execute(
            "SELECT profile_id, included_profile_id FROM profile_includes ORDER BY position"
        ):
            graph.setdefault(profile_id, []).append(included_id)

        visited: set[str] = set()
        active: set[str] = set()

        def visit(profile_id: str) -> None:
            if profile_id in active:
                name = connection.execute(
                    "SELECT name FROM profiles WHERE id = ?", (profile_id,)
                ).fetchone()[0]
                raise ValueError(f"La composición contiene un ciclo en el perfil '{name}'.")
            if profile_id in visited:
                return
            active.add(profile_id)
            for included_id in graph.get(profile_id, []):
                visit(included_id)
            active.remove(profile_id)
            visited.add(profile_id)

        for profile_id in [row[0] for row in connection.execute("SELECT id FROM profiles")]:
            visit(profile_id)

    def validate(self) -> list[str]:
        with self.database.connect() as connection:
            self._validate_graph(connection)
        resolution = self.resolve_active()
        return resolution.warnings if resolution else []

    def activation(self) -> Activation | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT primary_profile_id FROM profile_activation WHERE singleton = 1"
            ).fetchone()
            if row is None:
                return None
            primary = connection.execute(
                "SELECT name FROM profiles WHERE id = ?", (row[0],)
            ).fetchone()
            if primary is None:
                raise ValueError(
                    "La activación local referencia un perfil inexistente; corríjala con "
                    "concord profile activate o concord profile deactivate --all."
                )
            complements = [
                item
                for item in connection.execute(
                    "SELECT profile_id FROM profile_activation_complements ORDER BY position"
                )
            ]
            complement_names = []
            for (profile_id,) in complements:
                item = connection.execute(
                    "SELECT name FROM profiles WHERE id = ?", (profile_id,)
                ).fetchone()
                if item is None:
                    raise ValueError(
                        "La activación local contiene complementos inexistentes; "
                        "corríjala con concord profile activate o concord profile deactivate --all."
                    )
                complement_names.append(item[0])
        return Activation(primary[0], complement_names)

    def _normalize_activation(
        self, primary: str, complements: list[str] | None = None
    ) -> Activation:
        complements = complements or []
        primary_name = primary.strip().lower()
        ordered = []
        for name in complements:
            normalized = name.strip().lower()
            if normalized in ordered:
                ordered.remove(normalized)
            ordered.append(normalized)
        if primary_name in ordered:
            raise ValueError("El perfil principal no puede ser también un complemento.")
        return Activation(primary_name, ordered)

    def activate(self, primary: str, complements: list[str] | None = None) -> Activation:
        activation = self._normalize_activation(primary, complements)
        with self.database.connect() as connection:
            primary_id = self._id(connection, "profiles", activation.primary, "un perfil")
            complement_ids = [
                self._id(connection, "profiles", name, "un perfil")
                for name in activation.complements
            ]
            connection.execute("DELETE FROM profile_activation_complements")
            connection.execute("DELETE FROM profile_activation")
            connection.execute(
                "INSERT INTO profile_activation VALUES (1, ?)", (primary_id,)
            )
            connection.executemany(
                "INSERT INTO profile_activation_complements VALUES (?, ?)",
                [(profile_id, position) for position, profile_id in enumerate(complement_ids)],
            )
        return self.activation()  # type: ignore[return-value]

    def deactivate_all(self) -> None:
        with self.database.connect() as connection:
            connection.execute("DELETE FROM profile_activation_complements")
            connection.execute("DELETE FROM profile_activation")

    def deactivate(self, name: str, *, replace_with: str | None = None) -> Activation | None:
        active = self.activation()
        if active is None:
            return None
        normalized = name.strip().lower()
        if normalized == active.primary:
            if replace_with is None:
                raise ValueError("Indique el nuevo perfil principal con --replace-with o use --all.")
            replacement = replace_with.strip().lower()
            complements = [item for item in active.complements if item != replacement]
            return self.activate(replacement, complements)
        if normalized not in active.complements:
            raise ValueError(f"El perfil '{normalized}' no está activo.")
        return self.activate(active.primary, [item for item in active.complements if item != normalized])

    def _apply_profile(
        self,
        connection,
        profile_id: str,
        ordered: dict[str, None],
        protected: set[str],
        warnings: list[str],
        applied_exclusions: list[str],
        stack: list[str],
    ) -> None:
        if profile_id in stack:
            raise ValueError("La composición de perfiles contiene un ciclo.")
        stack.append(profile_id)
        profile_name = connection.execute(
            "SELECT name FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()[0]
        included_ids = [
            row[0]
            for row in connection.execute(
                "SELECT included_profile_id FROM profile_includes WHERE profile_id = ? ORDER BY position",
                (profile_id,),
            )
        ]
        for included_id in included_ids:
            self._apply_profile(
                connection, included_id, ordered, protected, warnings, applied_exclusions, stack
            )
        target_ids = [
            row[0]
            for row in connection.execute(
                "SELECT target_id FROM profile_targets WHERE profile_id = ? ORDER BY position",
                (profile_id,),
            )
        ]
        for target_id in target_ids:
            ordered.setdefault(target_id, None)
        excluded_ids = [
            row[0]
            for row in connection.execute(
                "SELECT target_id FROM profile_exclusions WHERE profile_id = ? ORDER BY position",
                (profile_id,),
            )
        ]
        for target_id in excluded_ids:
            target_name = connection.execute(
                "SELECT name FROM targets WHERE id = ?", (target_id,)
            ).fetchone()[0]
            if target_id in protected:
                warnings.append(
                    f"'{profile_name}' no puede excluir el target protegido '{target_name}'."
                )
            elif target_id in ordered:
                del ordered[target_id]
                if target_name not in applied_exclusions:
                    applied_exclusions.append(target_name)
            else:
                warnings.append(
                    f"La exclusión de '{target_name}' en '{profile_name}' no tuvo coincidencia."
                )
        stack.pop()

    def resolve_active(self) -> ProfileResolution | None:
        activation = self.activation()
        if activation is None:
            return None
        return self.resolve_activation(activation.primary, activation.complements)

    def resolve_activation(
        self, primary: str, complements: list[str] | None = None
    ) -> ProfileResolution:
        activation = self._normalize_activation(primary, complements)
        with self.database.connect() as connection:
            primary_id = self._id(connection, "profiles", activation.primary, "un perfil")
            ordered: dict[str, None] = {}
            warnings: list[str] = []
            applied_exclusions: list[str] = []
            self._apply_profile(
                connection, primary_id, ordered, set(), warnings, applied_exclusions, []
            )
            protected = set(ordered)
            for complement in activation.complements:
                complement_id = self._id(connection, "profiles", complement, "un perfil")
                self._apply_profile(
                    connection, complement_id, ordered, protected, warnings,
                    applied_exclusions, []
                )
            names = []
            for target_id in ordered:
                row = connection.execute(
                    "SELECT name FROM targets WHERE id = ?", (target_id,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"La activación referencia un target inexistente: {target_id}.")
                names.append(row[0])
        return ProfileResolution(
            list(ordered), names, warnings, activation, applied_exclusions
        )

    def resolve(self, name: str) -> ProfileResolution:
        profile = self.get(name)
        with self.database.connect() as connection:
            ordered: dict[str, None] = {}
            warnings: list[str] = []
            applied_exclusions: list[str] = []
            self._apply_profile(
                connection, profile.id, ordered, set(), warnings, applied_exclusions, []
            )
            names = [
                connection.execute("SELECT name FROM targets WHERE id = ?", (target_id,)).fetchone()[0]
                for target_id in ordered
            ]
        return ProfileResolution(
            list(ordered), names, warnings, Activation(profile.name, []), applied_exclusions
        )

    def suggest(self, primary: str, complements: list[str] | None = None) -> Activation:
        complements = complements or []
        primary_name = primary.strip().lower()
        ordered = []
        for name in complements:
            normalized = name.strip().lower()
            if normalized in ordered:
                ordered.remove(normalized)
            ordered.append(normalized)
        if primary_name in ordered:
            raise ValueError("El perfil principal no puede ser también un complemento.")
        with self.database.connect() as connection:
            primary_id = self._id(connection, "profiles", primary_name, "un perfil")
            ids = [self._id(connection, "profiles", name, "un perfil") for name in ordered]
            connection.execute("DELETE FROM profile_suggestion_complements")
            connection.execute("DELETE FROM profile_suggestion")
            connection.execute("INSERT INTO profile_suggestion VALUES (1, ?)", (primary_id,))
            connection.executemany(
                "INSERT INTO profile_suggestion_complements VALUES (?, ?)",
                [(profile_id, position) for position, profile_id in enumerate(ids)],
            )
            self._save_manifest(connection)
        return Activation(primary_name, ordered)

    def suggestion(self) -> Activation | None:
        with self.database.connect() as connection:
            row = connection.execute(
                """
                SELECT profiles.name FROM profile_suggestion
                JOIN profiles ON profiles.id = profile_suggestion.primary_profile_id
                WHERE singleton = 1
                """
            ).fetchone()
            if row is None:
                return None
            complements = [
                item[0]
                for item in connection.execute(
                    """
                    SELECT profiles.name FROM profile_suggestion_complements
                    JOIN profiles ON profiles.id = profile_suggestion_complements.profile_id
                    ORDER BY position
                    """
                )
            ]
        return Activation(row[0], complements)

    def suggestion_fingerprint(self) -> str | None:
        suggestion = self.suggestion()
        if suggestion is None:
            return None
        value = json.dumps([suggestion.primary, suggestion.complements])
        return hashlib.sha256(value.encode()).hexdigest()

    def should_offer_suggestion(self) -> bool:
        if self.activation() is not None:
            return False
        fingerprint = self.suggestion_fingerprint()
        if fingerprint is None:
            return False
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT value FROM local_settings WHERE key = 'declined_profile_suggestion'"
            ).fetchone()
        return row is None or row[0] != fingerprint

    def decline_suggestion(self) -> None:
        fingerprint = self.suggestion_fingerprint()
        if fingerprint is None:
            return
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO local_settings (key, value) VALUES ('declined_profile_suggestion', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (fingerprint,),
            )

    def apply_to_config(self, config: Config, *, connection=None) -> None:
        owns_connection = connection is None
        if owns_connection:
            connection = self.database.connect()
        try:
            target_ids = {
                name: target_id
                for target_id, name in connection.execute("SELECT id, name FROM targets")
            }
            config.targets = [
                replace(target, id=target_ids.get(target.name, target.id))
                for target in config.targets
            ]
            profile_rows = connection.execute(
                "SELECT id, name, description FROM profiles ORDER BY name"
            ).fetchall()

            def references(table: str, column: str, joined: str, profile_id: str):
                return [
                    ManifestReference(item[0], item[1])
                    for item in connection.execute(
                        f"""
                        SELECT {joined}.id, {joined}.name FROM {table}
                        JOIN {joined} ON {joined}.id = {table}.{column}
                        WHERE {table}.profile_id = ? ORDER BY {table}.position
                        """,
                        (profile_id,),
                    )
                ]

            config.profiles = []
            for profile_id, name, description in profile_rows:
                config.profiles.append(
                    ProfileConfig(
                        id=profile_id,
                        name=name,
                        description=description,
                        includes=references(
                            "profile_includes", "included_profile_id", "profiles", profile_id
                        ),
                        targets=references(
                            "profile_targets", "target_id", "targets", profile_id
                        ),
                        excludes=references(
                            "profile_exclusions", "target_id", "targets", profile_id
                        ),
                    )
                )
            suggestion = self._suggestion_from_connection(connection)
            config.suggested_activation = suggestion
            config.minimum_concord_version = (
                PROFILE_MINIMUM_VERSION if config.profiles else None
            )
        finally:
            if owns_connection:
                connection.close()

    def matches_config(self, config: Config) -> bool:
        local = Config(repository_path=config.repository_path)
        self.apply_to_config(local)
        return (
            local.profiles == config.profiles
            and local.suggested_activation == config.suggested_activation
        )

    def _suggestion_from_connection(self, connection) -> SuggestedActivationConfig | None:
        row = connection.execute(
            """
            SELECT profiles.id, profiles.name FROM profile_suggestion
            JOIN profiles ON profiles.id = profile_suggestion.primary_profile_id
            WHERE singleton = 1
            """
        ).fetchone()
        if row is None:
            return None
        complements = [
            ManifestReference(item[0], item[1])
            for item in connection.execute(
                """
                SELECT profiles.id, profiles.name FROM profile_suggestion_complements
                JOIN profiles ON profiles.id = profile_suggestion_complements.profile_id
                ORDER BY position
                """
            )
        ]
        return SuggestedActivationConfig(ManifestReference(row[0], row[1]), complements)

    def import_config(self, config: Config, *, connection=None) -> None:
        owns_connection = connection is None
        if owns_connection:
            connection = self.database.connect()
        try:
            connection.execute("DELETE FROM profiles")
            target_rows = connection.execute("SELECT id, name FROM targets").fetchall()
            target_ids = {row[0] for row in target_rows}
            concord_target_ids = {row[0] for row in target_rows if row[1] == CONCORD_TARGET}
            for profile in config.profiles:
                connection.execute(
                    "INSERT INTO profiles VALUES (?, ?, ?)",
                    (profile.id, self._normalize_name(profile.name), profile.description),
                )
            profile_ids = {row[0] for row in connection.execute("SELECT id FROM profiles")}
            for profile in config.profiles:
                if any(reference.id not in profile_ids for reference in profile.includes):
                    raise ValueError(f"El perfil '{profile.name}' incluye una referencia inexistente.")
                if any(
                    reference.id not in target_ids
                    for reference in [*profile.targets, *profile.excludes]
                ):
                    raise ValueError(f"El perfil '{profile.name}' referencia un target inexistente.")
                if any(
                    reference.id in concord_target_ids
                    for reference in [*profile.targets, *profile.excludes]
                ):
                    raise ValueError(
                        "El target interno 'concord' no puede pertenecer a un perfil."
                    )
                connection.executemany(
                    "INSERT INTO profile_includes VALUES (?, ?, ?)",
                    [
                        (profile.id, reference.id, position)
                        for position, reference in enumerate(profile.includes)
                    ],
                )
                connection.executemany(
                    "INSERT INTO profile_targets VALUES (?, ?, ?)",
                    [
                        (profile.id, reference.id, position)
                        for position, reference in enumerate(profile.targets)
                    ],
                )
                connection.executemany(
                    "INSERT INTO profile_exclusions VALUES (?, ?, ?)",
                    [
                        (profile.id, reference.id, position)
                        for position, reference in enumerate(profile.excludes)
                    ],
                )
            connection.execute("DELETE FROM profile_suggestion_complements")
            connection.execute("DELETE FROM profile_suggestion")
            if config.suggested_activation:
                primary_id = config.suggested_activation.primary.id
                if primary_id not in profile_ids:
                    raise ValueError("La activación sugerida referencia un perfil inexistente.")
                connection.execute("INSERT INTO profile_suggestion VALUES (1, ?)", (primary_id,))
                connection.executemany(
                    "INSERT INTO profile_suggestion_complements VALUES (?, ?)",
                    [
                        (reference.id, position)
                        for position, reference in enumerate(
                            config.suggested_activation.complements
                        )
                    ],
                )
            self._validate_graph(connection)
        finally:
            if owns_connection:
                connection.close()
