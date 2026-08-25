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
concord sync              # todos los targets
concord sync nvim         # solo uno
concord restore nvim      # exige que la ruta local no exista
concord restore nvim -f   # reemplaza la ruta local
concord remove nvim       # conserva los archivos locales
```

`add` acepta únicamente rutas dentro de `$HOME`. Tanto el nombre como la ruta
local son únicos: una misma configuración no puede registrarse dos veces con
nombres distintos. Los nombres que empiezan con punto se normalizan con el
prefijo `dot_`.

## Estados

- `clean`: la copia local y el repositorio coinciden.
- `modified`: hay cambios locales pendientes de `sync`.
- `missing`: la ruta local ya no existe y puede recuperarse con `restore`.
- `untracked`: falta la copia almacenada en el repositorio.

## Desarrollo

```bash
uv sync --dev
uv run pytest
```
