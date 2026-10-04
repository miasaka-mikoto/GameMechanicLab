#!/usr/bin/env bash
set -euo pipefail

# A dependency-light portable build for Linux/macOS development machines.
# Windows users should run build_windows.ps1 instead.  The generated folder
# keeps the source modules and data files together so it can be copied as-is.
project_root="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
out_dir="$project_root/dist/GameMechanicLab-portable"

rm -rf "$out_dir"
mkdir -p "$out_dir"

copy_if_present() {
  if [[ -e "$project_root/$1" ]]; then
    cp -R "$project_root/$1" "$out_dir/"
  fi
}

copy_if_present app.py
copy_if_present gamemechaniclab
copy_if_present src
copy_if_present configs
copy_if_present demo
copy_if_present docs
copy_if_present tests
copy_if_present README.md
copy_if_present pyproject.toml
copy_if_present requirements.txt
copy_if_present requirements-dev.txt
copy_if_present build_windows.ps1
copy_if_present build_portable.sh
copy_if_present GameMechanicLab.spec
copy_if_present LICENSE
cp "$project_root/run_demo.py" "$out_dir/"

# Keep generated Python bytecode and local test caches out of the portable
# handoff; they are not needed at runtime and can otherwise make the bundle
# look like a development checkout.
find "$out_dir" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "$out_dir" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

cat > "$out_dir/run.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname -- "$0")"
export PYTHONDONTWRITEBYTECODE=1
# Keep optional Matplotlib's font/config cache out of read-only home
# directories (common in containers and locked-down CI runners).
if [[ -z "${MPLCONFIGDIR:-}" ]]; then
  export MPLCONFIGDIR="${TMPDIR:-/tmp}/game-mechanic-lab-mplconfig"
fi
mkdir -p "$MPLCONFIGDIR"
exec "${PYTHON:-python3}" run_demo.py "$@"
EOF
chmod +x "$out_dir/run.sh"

echo "Portable build: $out_dir"
echo "Run with: $out_dir/run.sh"
