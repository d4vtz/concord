# Maintainer: David Torrez Reyes <davidtorrezreyes@gmail.com>

pkgname=concord
pkgver=2.3.1
pkgrel=3
pkgdesc='Gestor explícito y seguro de dotfiles con integración Git'
arch=('any')
url='https://github.com/d4vtz/concord'
license=('MIT')
depends=(
    'git'
    'python'
    'python-platformdirs'
    'python-questionary'
    'python-rich'
    'python-tomli-w'
    'python-typer'
)
makedepends=(
    'python-build'
    'python-installer'
    'python-uv-build'
)
checkdepends=('python-pytest')
optdepends=('github-cli: crear y autenticar repositorios remotos en GitHub')
_commit='3b214a352ac28f1845096a3e01778a818633ce5d'
source=("${pkgname}::git+${url}.git#commit=${_commit}")
b2sums=('SKIP')

build() {
    cd "$pkgname"
    python -m build --wheel --no-isolation
}

check() {
    cd "$pkgname"
    PYTHONPATH=src python -m pytest -o addopts=''
}

package() {
    cd "$pkgname"
    python -m installer --destdir="$pkgdir" dist/*.whl
    install -Dm644 LICENSE -t "$pkgdir/usr/share/licenses/$pkgname/"

    python -c 'import sys; from typer.completion import get_completion_script; print(get_completion_script(prog_name="concord", complete_var="_CONCORD_COMPLETE", shell=sys.argv[1]))' bash > concord.bash
    python -c 'import sys; from typer.completion import get_completion_script; print(get_completion_script(prog_name="concord", complete_var="_CONCORD_COMPLETE", shell=sys.argv[1]))' zsh > _concord
    python -c 'import sys; from typer.completion import get_completion_script; print(get_completion_script(prog_name="concord", complete_var="_CONCORD_COMPLETE", shell=sys.argv[1]))' fish > concord.fish
    install -Dm644 concord.bash "$pkgdir/usr/share/bash-completion/completions/concord"
    install -Dm644 _concord "$pkgdir/usr/share/zsh/site-functions/_concord"
    install -Dm644 concord.fish "$pkgdir/usr/share/fish/vendor_completions.d/concord.fish"
}
