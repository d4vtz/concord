# Concord

Concord es un gestor local de dotfiles para Linux. Conserva cada target en un
repositorio legible, manteniendo su ruta relativa a `$HOME`.

```text
~/.config/nvim  →  repository/nvim/.config/nvim
~/.bashrc       →  repository/dot_bashrc/.bashrc
```

## Instalación

Requiere Python 3.12 o posterior y [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/d4vtz/concord.git
cd concord
uv tool install .
concord init
```

## Uso

```bash
concord add ~/.bashrc
concord add ~/.config/nvim --name nvim
concord list
concord status
concord diff              # compara todos los targets
concord diff nvim         # compara un target sin modificar nada
concord sync              # todos los targets
concord sync nvim         # solo uno
concord restore nvim      # exige que la ruta local no exista
concord restore nvim -f   # reemplaza la ruta local
concord remove nvim       # conserva los archivos locales
concord import --replace  # reconstruye SQLite desde el manifiesto
concord restore --all     # restaura todos los targets
```

`add` acepta únicamente rutas dentro de `$HOME`. Tanto el nombre como la ruta
local son únicos: una misma configuración no puede registrarse dos veces con
nombres distintos. Los nombres que empiezan con punto se normalizan con el
prefijo `dot_`.

## Manifiesto portable

`~/.config/concord/concord.toml` es la fuente de verdad de Concord. Contiene la
configuración y la lista portable de targets:

```toml
version = 1
repository_path = "~/.local/share/concord/repository"

targets = [
    { name = "concord", relative_path = ".config/concord", type = "directory", created_at = "2026-08-25T12:00:00+00:00", updated_at = "2026-08-25T12:00:00+00:00" },
    { name = "nvim", relative_path = ".config/nvim", type = "directory", created_at = "2026-08-25T12:05:00+00:00", updated_at = "2026-08-25T14:30:00+00:00" },
]
```

Concord se registra automáticamente como el primer target. Después de agregar
o eliminar una configuración, actualiza el manifiesto y sincroniza su propia
copia en `repository/concord/.config/concord/concord.toml`. La base SQLite es un
índice local reconstruible, no la fuente de verdad.

Cada target conserva `created_at`, la fecha en que fue registrado, y
`updated_at`, la última vez que Concord lo sincronizó. `concord list` muestra
ambas fechas en la zona horaria local.

## Recuperar configuraciones en otra máquina

Clona o copia tu repositorio y ejecuta:

```bash
concord init --repository ~/.local/share/concord/repository
concord restore --all
```

Si Concord ya estaba inicializado pero necesitas reconstruir el índice local:

```bash
concord import --replace
concord restore --all
```

Durante la importación, las rutas relativas del manifiesto se resuelven usando
el `$HOME` de la máquina actual. El target `concord` es reservado y no puede
eliminarse.

## Estados

- `clean`: la copia local y el repositorio coinciden.
- `modified`: hay cambios locales pendientes de `sync`.
- `missing`: la ruta local ya no existe y puede recuperarse con `restore`.
- `untracked`: falta la copia almacenada en el repositorio.

## Revisar cambios antes de sincronizar

`concord diff [target]` compara `$HOME` con el repositorio desde la perspectiva
de `sync`. Informa qué rutas serían agregadas, modificadas o eliminadas:

```text
● Modificado  .config/nvim/init.lua
+ Agregado    .config/nvim/lua/plugins.lua
− Eliminado   .config/nvim/lua/old.lua
```

Sin argumento compara todos los targets. El comando es de solo lectura: no
copia archivos ni cambia `updated_at`. También compara el destino de los enlaces
simbólicos y detecta directorios vacíos.

## Desarrollo

```bash
uv sync --dev
uv run pytest
```
