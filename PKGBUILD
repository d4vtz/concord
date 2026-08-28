# Maintainer: David Torrez Reyes <davidtorrezreyes@gmail.com>

pkgname=concord
pkgver=2.1.0
pkgrel=4
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
_commit='ed8e8d10aee31448d2ef8c487bf89af55d26f47a'
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
}
