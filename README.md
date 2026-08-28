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
concord edit nvim         # abre el target local sin sincronizarlo
concord edit ignore       # edita .gitignore, confirma y publica el cambio
concord edit ignore --no-push  # conserva localmente el commit
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

Los nombres `ignore`, `manifest` y `config` están reservados para recursos
internos de Concord y no pueden utilizarse como nombres de targets.

## Editar configuraciones

`concord edit <target>` abre la ruta local original. Los directorios se abren
como raíz del editor y los archivos desde su directorio padre. Este comando no
ejecuta `sync`, no cambia la copia del repositorio y no crea commits; permite
probar una configuración antes de sincronizarla explícitamente.

Concord usa `$VISUAL`, después `$EDITOR` y, si ninguna variable está definida,
busca `nvim`, `vim`, `vi` o `nano`. Los comandos configurados pueden incluir
argumentos, por ejemplo `VISUAL="nvim -f"` o `EDITOR="code --wait"`.

`concord edit ignore` es una operación especial. Exige un repositorio limpio,
abre o crea `.gitignore` y, si cambia, deja de rastrear los archivos que ahora
coincidan con sus reglas sin borrarlos del disco. Después crea el commit
`concord: update ignore rules` y lo envía al remoto configurado. No utiliza
force-push ni integra automáticamente cambios remotos. `--no-push` conserva el
commit únicamente en la máquina local.

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
nvim: sync target
concord: sync all targets
concord: remove nvim
```

El mensaje de `sync` depende de los targets realmente modificados: si solo uno
cambia se utiliza `<target>: sync target`; si cambian dos o más se utiliza
`concord: sync all targets`. Una sincronización limpia no crea ningún commit.

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

## Implementaciones pendientes

- Implementar completado dinámico de shell para los nombres registrados. Al
  completar argumentos de comandos como `sync`, `restore`, `remove`, `diff` o
  `edit`, Concord deberá consultar los targets locales y mostrar únicamente los
  que coincidan con el texto escrito.

  ```text
  concord sync nv<Tab>       -> nvim
  concord edit <Tab>         -> bash  git  kitty  nvim  zsh
  concord profile show <Tab> -> base  kde  qtile
  ```

  La instalación aprovechará el mecanismo de completado de Typer:

  ```bash
  concord --install-completion zsh
  concord --install-completion bash
  concord --install-completion fish
  ```

  El completado deberá ser de solo lectura, no consultar la red, no inicializar
  Concord ni imprimir errores cuando todavía no existan configuración,
  manifiesto o base de datos. Cada sugerencia podrá incluir una descripción
  breve con la ruta o el tipo del target. Los comandos de perfiles deberán
  completar nombres de perfiles o targets según el argumento esperado; por
  ejemplo, `profile add` sugerirá targets y `profile restore` sugerirá perfiles.
  La consulta deberá ser suficientemente ligera para ejecutarse en cada
  pulsación de tabulador y cerrar siempre sus conexiones a SQLite.
### Targets con múltiples rutas

```bash
concord add ~/.config/zsh --name zsh
concord add ~/.zshenv --name zsh
```

Comportamiento esperado:

- Crear el target si todavía no existe.
- Reutilizar el target cuando el valor de `--name` coincida.
- Permitir que un target contenga varias rutas.
- Impedir que una misma ruta pertenezca a dos targets distintos.
- Replicar todas las rutas conservando su ubicación relativa a `$HOME`.

### Cifrado de archivos sensibles

Implementar cifrado mediante `age`, evitando algoritmos o formatos propios.
El archivo original permanecerá descifrado en el sistema del usuario, pero el
repositorio almacenará únicamente su versión cifrada con la extensión `.age`.

Flujo propuesto:

```bash
concord add ~/.ssh/config --name ssh --encrypt
concord sync ssh
concord restore ssh
```

También deberá ser posible cambiar el estado de un archivo ya registrado:

```bash
concord encrypt ssh .ssh/config
concord decrypt ssh .ssh/config
```

Comportamiento esperado:

- Registrar en el manifiesto qué rutas están cifradas y los destinatarios
  públicos de `age` utilizados, pero nunca una identidad o clave privada.
- Cifrar antes de copiar al repositorio durante `add` y `sync`; ningún archivo
  temporal en texto plano deberá quedar dentro del repositorio.
- Descifrar durante `restore` directamente hacia un archivo temporal seguro y
  reemplazar el destino solamente cuando la operación termine correctamente.
- Conservar permisos, ruta relativa a `$HOME` y pertenencia al target.
- Admitir uno o varios destinatarios para poder restaurar desde distintas
  máquinas y permitir rotación de claves.
- Obtener la identidad privada desde una ruta configurada fuera del repositorio
  o desde un agente compatible; nunca solicitar que se confirme en Git.
- Hacer que `bootstrap` detecte archivos cifrados, compruebe la identidad antes
  de restaurarlos y continúe con los archivos no cifrados si el usuario decide
  omitirlos.
- Mostrar en `status`, `doctor` y `--dry-run` qué archivos están cifrados sin
  revelar su contenido ni material criptográfico privado.
- Bloquear `decrypt` si convertiría el repositorio a texto plano sin una
  confirmación explícita del usuario.
- Mantener la detección de secretos como una defensa adicional: un archivo
  sensible que no esté cifrado debe seguir generando una advertencia antes del
  primer push.

La primera versión utilizará cifrado por destinatario de `age`; el cifrado con
contraseña podrá evaluarse después, ya que dificulta la automatización segura de
`sync`, `restore` y `bootstrap`.

### Perfiles de configuración

Implementar perfiles para agrupar targets que forman parte de una misma
configuración. Esto permitirá instalar primero una configuración base y después
componer sobre ella un entorno específico, como KDE, Qtile o un servidor.

Ejemplo propuesto:

```bash
concord profile create base
concord profile add base git ssh zsh

concord profile create kde --include base
concord profile add kde konsole plasma chrome

concord profile restore kde
```

En este ejemplo, restaurar `kde` aplicará los targets de `base` y luego los
targets propios de `kde`.

Comandos previstos:

```bash
concord profile list
concord profile show <profile>
concord profile create <profile>
concord profile add <profile> <target>...
concord profile remove <profile> <target>...
concord profile include <profile> <profile-base>...
concord profile sync <profile>
concord profile restore <profile>
concord profile delete <profile>
```

Comportamiento esperado:

- Un perfil contendrá referencias a targets existentes, no copias de sus
  archivos ni targets nuevos.
- Un mismo target podrá pertenecer a varios perfiles para reutilizar una
  configuración común, sin permitir que una ruta pertenezca a dos targets.
- Los perfiles podrán incluir otros perfiles para componer configuraciones por
  capas, como `base` + `kde` o `base` + `qtile`.
- Concord rechazará inclusiones circulares entre perfiles.
- La composición tendrá un orden determinista: primero los perfiles incluidos,
  en el orden declarado, y después los targets propios.
- Si un target aparece más de una vez durante la composición, se procesará una
  sola vez.
- `profile sync` y `profile restore` admitirán `--dry-run` y mostrarán el orden
  exacto de los targets antes de modificar archivos.
- `profile restore` conservará las mismas confirmaciones y protecciones del
  comando `restore`, incluido el tratamiento de archivos cifrados.
- El manifiesto guardará la definición de los perfiles para que `bootstrap`
  pueda reconstruirlos; SQLite seguirá siendo un índice local regenerable.
- Eliminar un perfil no eliminará sus targets ni sus archivos.
- `status` y `doctor` comprobarán targets inexistentes, perfiles vacíos y ciclos
  de composición.

Más adelante podrá añadirse un perfil activo por máquina, pero la primera
versión no sincronizará automáticamente el nombre del equipo ni decidirá qué
perfil restaurar sin confirmación.

### Dependencias de paquetes por target

Permitir que cada target declare los paquetes necesarios para que su
configuración funcione. En una instalación nueva, Concord podrá reunir las
dependencias de uno o varios targets —o de un perfil completo—, comprobar cuáles
faltan e instalarlas mediante el gestor correspondiente.

Ejemplo propuesto para Arch Linux:

```bash
concord deps add nvim --manager pacman neovim ripgrep fd
concord deps add nvim --manager aur lua-language-server
concord deps add zsh --manager pacman zsh fzf

concord deps list nvim
concord deps check nvim
concord deps install nvim --dry-run
concord profile deps install base
```

Comandos previstos:

```bash
concord deps list <target>
concord deps add <target> --manager <manager> <package>...
concord deps remove <target> --manager <manager> <package>...
concord deps check <target>
concord deps install <target>
concord profile deps list <profile>
concord profile deps check <profile>
concord profile deps install <profile>
```

Comportamiento esperado:

- Guardar en el manifiesto los nombres de paquetes agrupados por gestor; SQLite
  seguirá siendo un índice local reconstruible.
- Comenzar con `pacman` para paquetes oficiales de Arch y un backend AUR
  configurable, inicialmente `paru` o `yay`, sin asumir que ambos existen.
- Diseñar una interfaz de backends que permita añadir posteriormente gestores
  como `apt`, `dnf`, `brew`, `flatpak` o `pipx` sin cambiar el modelo de los
  targets.
- Separar dependencias obligatorias y opcionales, de modo que la instalación de
  estas últimas requiera una opción explícita como `--include-optional`.
- Al operar sobre un perfil, expandir sus perfiles incluidos, reunir las
  dependencias de todos los targets y eliminar duplicados antes de consultar el
  sistema.
- Instalar únicamente paquetes faltantes y mostrar cuáles ya están presentes,
  cuáles se omitirán y qué backend se utilizará.
- Requerir confirmación antes de instalar y admitir `--dry-run`, `--yes` y modo
  no interactivo seguro. `restore` y `bootstrap` podrán ofrecer ejecutar la
  instalación, pero nunca hacerlo silenciosamente.
- Ejecutar cada gestor mediante argumentos estructurados, sin construir comandos
  con `shell=True` ni aceptar nombres de paquetes que puedan convertirse en
  opciones arbitrarias.
- No ejecutar todo Concord con privilegios elevados. Solo el gestor de paquetes
  podrá solicitar `sudo` cuando sea necesario; los helpers de AUR se ejecutarán
  como usuario normal.
- Hacer que `doctor` compruebe backends disponibles y dependencias faltantes sin
  instalar nada.
- Permitir que una dependencia tenga un nombre distinto según el sistema en una
  versión posterior, conservando inicialmente una implementación clara y
  centrada en Arch Linux.

Las dependencias de Python que forman parte interna de Concord seguirán
gestionándose mediante el paquete de la aplicación; esta función se reservará
para programas externos requeridos por los dotfiles.

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
