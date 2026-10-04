#!/usr/bin/env bash
set -euo pipefail

# Build the native Linux distribution of Game Mechanic Lab.
#
# The project remains usable with the Python standard library only, but the
# default build installs the optional chart/YAML packages so the generated
# binary contains the same rich heatmaps as the reference demo.  Pass
# --skip-optional-packages for a smaller fallback binary.

project_root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
venv_path="${GML_LINUX_BUILD_VENV:-$project_root/.venv-build-linux}"
python_bin="${PYTHON:-python3}"
out_dir="$project_root/dist"
binary_path="$out_dir/GameMechanicLab-linux"
build_dir="$project_root/build/linux"
skip_optional=0
skip_validation=0
clean=0

# PyInstaller imports Matplotlib while analysing the module graph.  Some
# managed Linux images expose a read-only $HOME, so never let a font/cache
# write there abort or slow a build.
if [[ -z "${MPLCONFIGDIR:-}" ]]; then
  export MPLCONFIGDIR="${TMPDIR:-/tmp}/game-mechanic-lab-mplconfig-build"
fi
mkdir -p "$MPLCONFIGDIR"

usage() {
  cat <<'EOF'
Usage: ./build_linux.sh [options]

Options:
  --clean                    Remove the Linux build directory and dist output.
  --skip-optional-packages  Build without Matplotlib/PyYAML.
  --skip-validation         Do not run the packaged one-fight smoke test.
  -h, --help                Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clean) clean=1 ;;
    --skip-optional-packages) skip_optional=1 ;;
    --skip-validation) skip_validation=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if ! command -v "$python_bin" >/dev/null 2>&1; then
  echo "Python interpreter not found: $python_bin" >&2
  exit 1
fi

python_version="$($python_bin -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if ! "$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Game Mechanic Lab requires Python 3.10 or newer; found $python_version" >&2
  exit 1
fi

if [[ "$clean" -eq 1 ]]; then
  rm -rf "$build_dir" "$out_dir/GameMechanicLab-linux" "$out_dir/GameMechanicLab-linux-build-info.json"
fi

if [[ ! -x "$venv_path/bin/python" ]]; then
  # System site packages make the builder usable in locked-down/offline Linux
  # workspaces where PyInstaller and the optional chart packages are already
  # provisioned by the host image.
  "$python_bin" -m venv --system-site-packages "$venv_path"
fi
build_python="$venv_path/bin/python"

has_modules() {
  "$build_python" - "$@" <<'PY' >/dev/null 2>&1
import importlib.util
import sys

missing = [name for name in sys.argv[1:] if importlib.util.find_spec(name) is None]
raise SystemExit(0 if not missing else 1)
PY
}

# A pre-existing venv from an older checkout may not expose system packages.
# Fall back to the selected host interpreter when it already has the complete
# toolchain; this keeps `--clean` useful without deleting a user's venv.
required_modules=(PyInstaller)
if [[ "$skip_optional" -eq 0 ]]; then
  required_modules+=(matplotlib yaml)
fi
if ! has_modules "${required_modules[@]}" && "$python_bin" - "${required_modules[@]}" <<'PY' >/dev/null 2>&1
import importlib.util
import sys

missing = [name for name in sys.argv[1:] if importlib.util.find_spec(name) is None]
raise SystemExit(0 if not missing else 1)
PY
then
  build_python="$python_bin"
fi

if ! has_modules PyInstaller; then
  "$build_python" -m pip install pyinstaller
fi
if [[ "$skip_optional" -eq 0 ]] && ! has_modules matplotlib yaml; then
  # Optional packages enrich charts and YAML editing.  A minimal build remains
  # valid when an offline host cannot fetch them; JSON and SVG paths are always
  # available.
  if ! "$build_python" -m pip install -r "$project_root/requirements.txt"; then
    echo "Warning: optional Linux packages unavailable; continuing without Matplotlib/PyYAML." >&2
    skip_optional=1
  fi
fi
pyinstaller_cmd=("$build_python" -m PyInstaller)

optional_exclude_args=()
if [[ "$skip_optional" -eq 1 ]]; then
  # Optional imports are deliberately guarded in the application.  Excluding
  # them here makes the documented fallback genuinely standard-library-first
  # even when the host interpreter happens to have Matplotlib installed.
  optional_exclude_args=(
    --exclude-module matplotlib
    --exclude-module numpy
    --exclude-module yaml
    --exclude-module PIL
  )
fi

mkdir -p "$out_dir"
data_args=(
  --add-data "$project_root/configs:configs"
  --add-data "$project_root/demo:demo"
  --add-data "$project_root/docs:docs"
)

"${pyinstaller_cmd[@]}" \
  --noconfirm \
  --clean \
  --onefile \
  --console \
  --name GameMechanicLab-linux \
  --distpath "$out_dir" \
  --workpath "$build_dir" \
  --specpath "$build_dir" \
  "${optional_exclude_args[@]}" \
  "${data_args[@]}" \
  "$project_root/app.py"

if [[ ! -f "$binary_path" ]]; then
  echo "PyInstaller completed without producing $binary_path" >&2
  exit 1
fi
chmod +x "$binary_path"

source_tree_hash="$({
  find "$project_root" \
    -path "$project_root/.git" -prune -o \
    -path "$project_root/.venv-build-linux" -prune -o \
    -path "$project_root/build" -prune -o \
    -path "$project_root/dist" -prune -o \
    -path "$project_root/__pycache__" -prune -o \
    -type f \( -name '*.py' -o -name '*.json' -o -name '*.yaml' -o -name '*.yml' -o -name '*.toml' -o -name '*.md' -o -name '*.sh' -o -name '*.txt' \) -print0
} | sort -z | while IFS= read -r -d '' file; do
  relative="${file#"$project_root/"}"
  printf '%s\n' "$relative"
  sha256sum "$file" | awk '{print $1}'
done | sha256sum | awk '{print $1}')"

build_python_version="$($build_python --version 2>&1)"
pyinstaller_version="$("${pyinstaller_cmd[@]}" --version 2>&1)"
architecture="$(uname -m)"
build_info_path="$out_dir/GameMechanicLab-linux-build-info.json"
BUILD_INFO_PATH="$build_info_path" \
BUILD_BINARY_PATH="$binary_path" \
BUILD_SOURCE_HASH="$source_tree_hash" \
BUILD_PYTHON="$build_python_version" \
BUILD_PYINSTALLER="$pyinstaller_version" \
BUILD_ARCH="$architecture" \
BUILD_OPTIONAL="$((1 - skip_optional))" \
"$build_python" - <<'PY'
import json
import os
from pathlib import Path

payload = {
    "schema": "game-mechanic-lab.build.v1",
    "product": "Game Mechanic Lab",
    "version": "0.1.0",
    "target": "linux",
    "architecture": os.environ["BUILD_ARCH"],
    "artifact": "dist/GameMechanicLab-linux",
    "python": os.environ["BUILD_PYTHON"],
    "pyinstaller": os.environ["BUILD_PYINSTALLER"],
    "optional_packages_installed": os.environ["BUILD_OPTIONAL"] == "1",
    "source_tree_sha256": os.environ["BUILD_SOURCE_HASH"],
}
Path(os.environ["BUILD_INFO_PATH"]).write_text(
    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
PY

if [[ "$skip_validation" -eq 0 ]]; then
  smoke_root="$(mktemp -d "${TMPDIR:-/tmp}/game-mechanic-lab-linux-smoke.XXXXXX")"
  trap 'rm -rf "$smoke_root"' EXIT
  "$binary_path" --headless --runs 1 --output "$smoke_root"
  SMOKE_ROOT="$smoke_root" "$build_python" - <<'PY'
import json
import os
from pathlib import Path

result = Path(os.environ["SMOKE_ROOT"]) / "simulation_result.json"
payload = json.loads(result.read_text(encoding="utf-8"))
if int(payload.get("runs", 0)) != 1 or not payload.get("summary"):
    raise SystemExit("Linux packaged smoke test returned an invalid result")
print("Linux packaged smoke test passed: one real headless fight.")
PY
fi

echo "Built: $binary_path"
echo "Build metadata: $build_info_path"
