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

### Arch Linux

Concord incluye un `PKGBUILD` para instalar el ejecutable mediante `pacman`. La
dependencia `python-questionary` está disponible en AUR, por lo que debe
instalarse primero con un helper:

```bash
sudo pacman -S --needed base-devel git
yay -S python-questionary
```

Después, desde el repositorio de Concord:

```bash
makepkg -si
concord --help
concord doctor
```

El paquete instala `concord` en `/usr/bin`, pero la configuración sigue siendo
individual para cada usuario. Ejecuta `concord init` sin `sudo`; usar `sudo`
crearía una configuración separada para `root`.

Para reconstruir el paquete después de actualizar el repositorio:

```bash
git pull origin master
makepkg -Csi
```

Durante `init`, Concord puede inicializar Git, configurar los commits
automáticos y crear un repositorio remoto con GitHub CLI (`gh`). Los repositorios
nuevos usan la rama `main`. Si falta la identidad global de Git, Concord solicita
un nombre y correo y los guarda únicamente en el repositorio de dotfiles.

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
concord sync nvim --dry-run  # simula HOME → repositorio
concord sync nvim -m "nvim: configura LSP"
concord sync nvim --no-push
concord restore nvim      # exige que la ruta local no exista
concord restore nvim -f   # reemplaza la ruta local
concord restore nvim --dry-run  # simula repositorio → HOME
concord remove nvim       # conserva los archivos locales
concord import --replace  # reconstruye SQLite desde el manifiesto
concord restore --all     # restaura todos los targets
concord restore --all --dry-run  # simula la restauración completa
concord repo status       # estado del repositorio Git
concord repo push         # publica commits locales
concord doctor            # diagnostica la instalación sin modificarla
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

## Simular operaciones

Agrega `--dry-run` a `sync` o `restore` para ver la operación completa antes de
aplicarla. La salida muestra los archivos que el comando agregaría, modificaría
o eliminaría, siempre desde la perspectiva del destino:

- `concord sync --dry-run`: `$HOME` → repositorio.
- `concord restore <target> --dry-run`: repositorio → `$HOME`.
- `concord restore --all --dry-run`: simula todos los targets restaurables sin
  incluir el target interno `concord`.

La simulación no copia ni elimina archivos, no modifica el manifiesto y no
actualiza `updated_at`. `--force` puede combinarse con `restore --dry-run` para
construir y revisar exactamente el comando que después se ejecutará.

## Integración con Git

El repositorio de Concord se inicializa automáticamente con Git. `init`, `add`,
`sync` y `remove` crean commits cuando producen cambios. Antes de cada commit
interactivo, Concord permite editar un mensaje predeterminado como:

```text
concord: add nvim
concord: sync nvim
concord: sync all targets
concord: remove nvim
```

Cada commit prepara exclusivamente las rutas afectadas por la operación actual;
los cambios pendientes de otros targets no se mezclan. Las opciones disponibles
son:

```bash
concord sync nvim --message "nvim: configura Python"
concord sync nvim --yes       # acepta el mensaje predeterminado
concord sync nvim --no-commit # deja los cambios sin commit
concord sync nvim --push      # fuerza push en esta operación
concord sync nvim --no-push   # omite push en esta operación
```

La configuración se guarda en el mismo manifiesto portable:

```toml
[git]
enabled = true
auto_commit = true
auto_push = true
remote = "origin"
```

En una ejecución sin terminal interactiva, Git y los commits quedan activos,
pero `auto_push` se configura inicialmente como `false`. Si un commit o push
falla, los archivos y commits completados se conservan. Concord nunca ejecuta
force-push, merge ni rebase automáticamente.

Antes del primer push se detectan nombres habituales de secretos, como `.env`,
claves privadas, credenciales y tokens. En modo interactivo se solicita
confirmación; en modo no interactivo el push se bloquea para permitir una
revisión manual.

## Administrar el repositorio

```bash
concord repo status             # estado local
concord repo status --fetch     # consulta también el remoto
concord repo log --limit 20
concord repo diff
concord repo diff --staged
concord repo commit -m "mensaje"
concord repo push
concord repo pull               # usa exclusivamente pull --ff-only
concord repo remote
concord repo remote set <URL>
concord repo remote remove
concord repo init               # inicializa o repara Git
```

`concord status` también muestra la rama, remoto, último commit y divergencia
con el upstream. Solo `--fetch` consulta la red.

## Diagnóstico

Antes de probar o después de migrar una instalación, ejecuta:

```bash
concord doctor
```

El diagnóstico es de solo lectura y comprueba:

- Validez de `concord.toml` y seguridad de sus rutas.
- Integridad de SQLite y coincidencia con el manifiesto.
- Existencia y sincronización de los targets.
- Instalación, identidad, rama y estado de Git.
- Configuración del remoto y seguimiento de la rama.
- Presencia de GitHub CLI y posibles archivos sensibles.

Por defecto no consulta la red. Para actualizar primero las referencias remotas:

```bash
concord doctor --fetch
```

Los errores producen un código de salida distinto de cero. Las advertencias son
informativas, salvo que se utilice el modo estricto:

```bash
concord doctor --strict
```

## Bootstrap desde GitHub

Para reconstruir Concord en otra máquina directamente desde el remoto:

```bash
concord bootstrap https://github.com/usuario/dotfiles.git
```

El comando clona el repositorio, recupera `concord.toml`, reconstruye SQLite y
ofrece restaurar todos los targets. También puede controlarse explícitamente:

```bash
concord bootstrap https://github.com/usuario/dotfiles.git --restore
concord bootstrap https://github.com/usuario/dotfiles.git --no-restore
```

## Desarrollo

```bash
uv sync --dev
uv run pytest
```
