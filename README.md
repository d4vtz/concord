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
concord add ~/.config/zsh --name zsh
concord add-path zsh ~/.zshenv
concord remove-path zsh ~/.zshenv
concord edit nvim         # abre el target local sin sincronizarlo
concord edit ignore       # edita .gitignore, confirma y publica el cambio
concord edit ignore --no-push  # conserva localmente el commit
concord sync nv<Tab>      # completa dinámicamente: nvim
concord list
concord list --all        # incluye targets fuera del perfil activo
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

`add` crea un target nuevo y acepta únicamente rutas dentro de `$HOME`. Para
agregar otra ruta a un target existente se usa `add-path`; `remove-path` deja de
administrarla sin borrarla de HOME. Ninguna ruta puede ser igual, padre o
descendiente de otra ruta registrada, incluso dentro del mismo target. Los
nombres que empiezan con punto se normalizan con el prefijo `dot_`.

Los nombres `ignore`, `manifest` y `config` están reservados para recursos
internos de Concord y no pueden utilizarse como nombres de targets.

## Perfiles

Los perfiles agrupan referencias a targets existentes sin duplicar sus archivos.
Permiten usar un mismo repositorio en equipos o contextos diferentes:

```bash
concord profile create base
concord profile edit base --target bash --target nvim

concord profile create linux
concord profile edit linux --include base --target qtile

concord profile create trabajo
concord profile edit trabajo --include base --target git
```

La edición sin opciones abre un selector interactivo:

```bash
concord profile edit linux
```

Una activación contiene un perfil principal y complementos ordenados. Sin
opciones, Concord guía la selección de la base, los complementos, su orden,
muestra una vista previa de targets y exclusiones, y pide confirmación:

```bash
concord profile activate
concord profile activate --primary linux --with trabajo
```

El principal forma una base protegida. Los complementos pueden agregar
targets y excluir targets aportados por complementos anteriores, pero no
pueden retirar los del principal. Las inclusiones se expanden primero, después
se agregan los targets directos y al final se aplican las exclusiones. Los ciclos
y referencias inexistentes se rechazan antes de guardar.

El target interno `concord` no aparece en los selectores y no puede agregarse
ni excluirse desde un perfil. `profile list` ofrece un resumen compacto;
`profile show <nombre>` conserva el árbol completo y el resultado expandido.

Con una activación, `list`, `status`, `diff`, `sync` y `restore --all` trabajan
por defecto sobre sus targets efectivos. Un target indicado explícitamente
continúa siendo válido aunque esté fuera del perfil. Para volver al comportamiento
global:

```bash
concord profile deactivate --all
```

Comandos de consulta y mantenimiento:

```bash
concord profile list
concord profile show linux
concord profile rename linux laptop
concord profile validate
concord profile suggest --primary linux --with trabajo
concord profile delete laptop
```

La activación efectiva se guarda únicamente en el equipo local. El manifiesto
puede llevar una combinación sugerida; Concord pregunta antes de adoptarla y,
si se rechaza, no vuelve a ofrecerla hasta que cambie.

## Dependencias de paquetes

Cada target puede declarar paquetes oficiales de Arch (`pacman`) y paquetes de
AUR. La ejecución sin argumentos abre el flujo guiado para elegir target,
origen, categoría y nombres:

```bash
concord deps add
```

También puede declararse todo desde la línea de comandos:

```bash
concord deps add nvim neovim ripgrep fd \
  --manager pacman --required
concord deps add nvim lua-language-server \
  --manager aur --optional
```

Concord valida que cada paquete exista antes de guardarlo. Para preparar el
manifiesto sin red puede utilizarse `--skip-validation`. Un mismo nombre no
puede pertenecer simultáneamente a `pacman` y AUR.

Comandos por target:

```bash
concord deps list nvim
concord deps check nvim
concord deps install nvim --dry-run
concord deps install nvim
concord deps install nvim --include-optional
concord deps remove nvim
```

Los perfiles expanden sus inclusiones y exclusiones, reúnen las dependencias de
sus targets y eliminan duplicados. Si un paquete es opcional en un target y
obligatorio en otro, prevalece como obligatorio:

```bash
concord profile deps list base
concord profile deps check base
concord profile deps install base --dry-run
concord profile deps install base
```

`check` termina con código distinto de cero únicamente cuando falta una
dependencia obligatoria. Las opcionales ausentes se muestran como advertencias.
`install` omite los paquetes ya satisfechos, permite elegir opcionales en una
terminal y requiere `--include-optional` para incluirlas todas de forma
explícita.

La preferencia entre `paru` y `yay` es local para cada máquina:

```bash
concord deps helper
concord deps helper paru
concord deps helper install
```

Cuando una instalación contiene paquetes AUR y no existe ningún helper, Concord
ofrece preparar `paru-bin` o `yay-bin`. Instala primero los prerrequisitos
faltantes con `pacman`, clona exclusivamente el repositorio HTTPS oficial de
AUR en un directorio temporal, muestra el PKGBUILD completo y exige confirmarlo
antes de ejecutar `makepkg -si`. Conserva los prompts nativos de `makepkg`,
`pacman` y `sudo`, limpia siempre el clon temporal y guarda la elección solo en
SQLite local después de verificar el ejecutable.

La preparación del helper está disponible desde `bootstrap`, `deps install`,
`profile deps install`, `restore --install-deps` y el comando independiente
`deps helper install`. Está prohibida en modo no interactivo, incluso con
`--yes`. Un `--dry-run` sin helper se limita a informar qué comando debe
ejecutarse desde una terminal.

Antes de instalar las dependencias normales, Concord muestra todos los lotes y
pide una única confirmación. En modo no interactivo exige `--yes`. Un fallo
posterior no desinstala paquetes ya agregados: informa lo completado y lo que
queda pendiente.

`deps remove` elimina solo la declaración del manifiesto; nunca desinstala
paquetes del sistema.

Antes de restaurar, Concord puede comprobar e instalar las dependencias del
target o de todos los targets activos:

```bash
concord restore nvim --install-deps
concord restore --all --install-deps
concord restore --all --install-deps --include-optional
```

En una terminal, si se omite `--install-deps`, Concord ofrece preparar la
instalación cuando encuentra paquetes faltantes. En automatización se omite por
defecto y debe autorizarse con `--install-deps --yes`. Si la instalación falla,
la restauración se detiene antes de modificar HOME. `--dry-run` muestra también
el plan de paquetes sin instalarlos.

`concord doctor` comprueba las declaraciones, `pacman`, el helper AUR y los
paquetes obligatorios. Las dependencias opcionales ausentes son informativas y
no convierten el diagnóstico en error.

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

## Completado de shell

Concord completa dinámicamente los targets registrados en `edit`, `diff`,
`sync`, `restore` y `remove`. Cada sugerencia muestra el nombre y su ruta local;
`edit` incluye también el recurso especial `ignore`. El target interno
`concord` se oculta en `remove`, donde no puede utilizarse.

El completado consulta primero `concord.toml`, la fuente de verdad, y utiliza el
índice SQLite como respaldo si el manifiesto no puede leerse. Ambas consultas
son de solo lectura. Los targets cuya ruta local no existe continúan apareciendo
para que puedan seleccionarse en `restore`. Si Concord aún no está inicializado
o ambas fuentes están dañadas, la shell no muestra sugerencias ni errores.

El paquete de Arch instala automáticamente las integraciones para Zsh, Bash y
Fish. Después de actualizar el paquete basta con reiniciar la shell o ejecutar
`rehash` en Zsh. Las instalaciones realizadas con `uv` pueden usar la opción
incorporada de Typer:

```bash
concord --install-completion
```

## Manifiesto portable

`~/.config/concord/concord.toml` es la fuente de verdad de Concord. Contiene la
configuración y la lista portable de targets:

```toml
version = 2
repository_path = "~/.local/share/concord/repository"

[[targets]]
id = "5e741dda-93bb-4e2a-a9c8-337a83ed755b"
name = "zsh"
created_at = "2026-08-25T12:05:00+00:00"
updated_at = "2026-08-25T14:30:00+00:00"
paths = [
    { relative_path = ".config/zsh", type = "directory" },
    { relative_path = ".zshenv", type = "file" },
]
dependencies = [
    { package = "zsh", manager = "pacman", optional = false },
    { package = "fzf", manager = "pacman", optional = true },
]
```

Los perfiles se guardan mediante UUID estables y nombres legibles. SQLite es la
fuente de trabajo para administrarlos y cada cambio exporta inmediatamente su
representación portable al manifiesto. Cuando el manifiesto contiene perfiles,
declara `minimum_concord_version = "2.3.1"`; al contener dependencias declara la
versión mínima que introdujo ese modelo.

Concord se registra automáticamente como el primer target. Después de agregar
o eliminar una configuración, actualiza el manifiesto y sincroniza su propia
copia en `repository/concord/.config/concord/concord.toml`. La base SQLite es un
índice local reconstruible, no la fuente de verdad.

Cada target conserva `created_at`, la fecha en que fue registrado, y
`updated_at`, la última vez que Concord lo sincronizó. `concord list` muestra
ambas fechas en la zona horaria local y cada ubicación relativa a `$HOME`. Los
targets se separan horizontalmente para distinguir sus grupos de rutas.

Al abrir por primera vez una instalación anterior, Concord migra
automáticamente el manifiesto v1 y SQLite al esquema v2. Antes de escribir crea
un respaldo fechado en `~/.local/share/concord/backups/`, actualiza la copia del
manifiesto y genera `concord: migrate manifest v2`, respetando `auto_push`.

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

`status` representa cada target como una sola unidad, aunque contenga varias
rutas. El estado general usa esta prioridad: falta local, falta copia,
modificado y limpio.

Para responder rápidamente, `status` compara primero estructura, tamaño y
`mtime_ns`, y lee únicamente archivos candidatos a haber cambiado. `diff`
mantiene la comparación profunda necesaria para enumerar cambios concretos.

`sync` y `restore` validan todas las rutas antes de escribir. Al operar sobre
todos los targets, un fallo impide modificar cualquiera. `restore` sin
`--force` aborta si existe al menos uno de los destinos; con `--force` reemplaza
el target completo. Las instalaciones físicas usan temporales y respaldos para
revertir cambios si falla el reemplazo.

## Revisar cambios antes de sincronizar

`concord diff` compara `$HOME` con el repositorio desde la perspectiva de
`sync`. Sin argumentos presenta un resumen de las rutas que serían agregadas,
modificadas o eliminadas:

```text
● Modificado  .config/nvim/init.lua
+ Agregado    .config/nvim/lua/plugins.lua
− Eliminado   .config/nvim/lua/old.lua
```

Sin argumento compara todos los targets. El comando es de solo lectura: no
copia archivos ni cambia `updated_at`. También compara el destino de los enlaces
simbólicos y detecta directorios vacíos.

Al indicar un target, muestra el contenido como un diff unificado: las líneas
`-` pertenecen al repositorio y las líneas `+` a HOME.

```bash
concord diff zsh
concord diff zsh --path ~/.zshenv
concord diff nvim --path ~/.config/nvim/init.lua --context 5
```

`--path` acepta una ruta registrada o cualquier archivo contenido en ella.
`--context` controla las líneas adyacentes mostradas y usa `3` por defecto.
Los archivos binarios, enlaces simbólicos, directorios vacíos y cambios de tipo
se resumen explícitamente sin intentar imprimir contenido ilegible.

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
- Integridad, composición y activación local de los perfiles.
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

Para medir qué bloques consumen más tiempo sin alterar el diagnóstico:

```bash
concord doctor --timings
```

La salida desglosa Configuración, SQLite, Perfiles, Targets y Git, además del
tiempo total. La medición usa un reloj monotónico y no modifica archivos.

## Implementaciones pendientes

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
concord bootstrap https://github.com/usuario/dotfiles.git --install-deps
```

Después de importar el manifiesto y resolver los perfiles activos, `bootstrap`
ofrece instalar sus paquetes antes de restaurar archivos. En modo no
interactivo debe indicarse explícitamente:

```bash
concord bootstrap https://github.com/usuario/dotfiles.git \
  --restore --install-deps --yes
```

La forma no interactiva solo funciona si ya existe un helper AUR preferido. Si
falta, la preparación de `paru-bin` o `yay-bin` debe completarse previamente en
una terminal con `concord deps helper install`; Concord nunca ejecuta un
PKGBUILD sin revisión interactiva.

Si existen configuraciones locales, `bootstrap` muestra las rutas afectadas y
pregunta si deben reemplazarse con las copias del repositorio. Rechazar la
confirmación conserva HOME sin cambios y deja Concord importado para restaurar
más tarde. En ejecuciones no interactivas, el reemplazo debe autorizarse de
forma explícita:

```bash
concord bootstrap https://github.com/usuario/dotfiles.git --restore --force
```

`--force` solo resuelve conflictos en HOME. Si falta la copia de un target en
el repositorio, debe sincronizarse desde el equipo original o eliminarse del
manifiesto.

## Reiniciar Concord

Para eliminar la configuración, SQLite, respaldos y repositorio local de
Concord sin tocar los targets restaurados en `$HOME` ni el remoto:

```bash
concord reset --dry-run
concord reset
```

La operación muestra y valida todas las rutas antes de borrar. En una terminal
exige escribir `RESET`; `--yes` permite confirmarla en pruebas automatizadas.
El paquete instalado por Arch permanece disponible para ejecutar nuevamente
`concord bootstrap <URL>`.

## Desarrollo

```bash
uv sync --dev
uv run pytest
```
