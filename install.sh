#!/usr/bin/env bash
# Errands installer - installs Errands CLI with orvima and Sentinel support
# Detects uv/pipx/pip and uses the best available installer

set -euo pipefail

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Parse flags FIRST
FORCE=false
NO_SENTINEL=false
NO_ORVIMA=false
POSITIONAL=()

while [[ $# -gt 0 ]]; do
    case $1 in
        --force) FORCE=true; shift ;;
        --no-sentinel) NO_SENTINEL=true; shift ;;
        --no-orvima) NO_ORVIMA=true; shift ;;
        -*) echo "Unknown option: $1"; exit 1 ;;
        *) POSITIONAL+=("$1"); shift ;;
    esac
done

# Now get version from positional args or default
VERSION="${POSITIONAL[0]:-latest}"

echo -e "${CYAN}╔══════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║            Errands Installer                ║${NC}"
echo -e "${CYAN}║   Narrow goals, fixed verified recipes      ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════╝${NC}"
echo ""

# Get latest version if not specified
if [[ "$VERSION" == "latest" ]]; then
    echo -e "${YELLOW}Fetching latest release...${NC}"
    VERSION=$(curl -s https://api.github.com/repos/Sachitt-AV-08/errands/releases/latest 2>/dev/null | grep '"tag_name"' | sed -E 's/.*"([^"]+)".*/\1/') || true
    if [[ -z "$VERSION" ]]; then
        echo -e "${RED}Failed to fetch latest release${NC}"
        VERSION="v0.1.0"
    fi
fi

echo -e "${GREEN}Version: $VERSION${NC}"

# Detect installer
if command -v uv >/dev/null 2>&1; then
    INSTALLER="uv"
elif command -v pipx >/dev/null 2>&1; then
    INSTALLER="pipx"
elif command -v pip >/dev/null 2>&1; then
    INSTALLER="pip"
else
    echo -e "${RED}No installer found. Please install uv, pipx, or pip.${NC}"
    exit 1
fi

echo -e "${CYAN}Using installer: $INSTALLER${NC}"

EXTRAS=()
if [[ "$NO_SENTINEL" != true ]]; then
    EXTRAS+=("sentinel")
fi
if [[ "$NO_ORVIMA" != true ]]; then
    EXTRAS+=("orvima")
fi

EXTRA=""
if [[ ${#EXTRAS[@]} -gt 0 ]]; then
    EXTRA="[$(IFS=,; echo "${EXTRAS[*]}")]"
fi

PKG="errands$EXTRA"

case $INSTALLER in
    uv)
        ARGS=("pip" "install" "$PKG")
        if [[ "$VERSION" != "latest" ]]; then
            ARGS+=("--index-url" "https://github.com/Sachitt-AV-08/errands/releases/download/$VERSION")
        fi
        echo -e "${CYAN}Running: uv ${ARGS[*]}${NC}"
        uv "${ARGS[@]}"
        ;;
    pipx)
        ARGS=("install" "$PKG")
        if [[ "$FORCE" == true ]]; then
            ARGS+=("--force")
        fi
        echo -e "${CYAN}Running: pipx ${ARGS[*]}${NC}"
        pipx "${ARGS[@]}"
        ;;
    pip)
        ARGS=("install" "$PKG")
        if [[ "$FORCE" == true ]]; then
            ARGS+=("--force-reinstall")
        fi
        echo -e "${CYAN}Running: pip ${ARGS[*]}${NC}"
        pip "${ARGS[@]}"
        ;;
esac

# Verify
echo ""
if python -c "import errands; print('Errands', errands.__version__)" 2>/dev/null; then
    echo -e "${GREEN}Errands installed successfully!${NC}"
else
    echo -e "${RED}Install verification failed: errands not importable${NC}"
    exit 1
fi