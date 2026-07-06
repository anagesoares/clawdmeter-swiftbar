#!/usr/bin/env bash
# CrabBar — instalador (macOS)
# Aponta o SwiftBar pra pasta plugins/, adiciona ao login e abre o app.
# Idempotente: pode rodar de novo sem duplicar nada.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$REPO_DIR/plugins"
echo "→ Pasta de plugins: $PLUGIN_DIR"

# 1. SwiftBar instalado?
if [ ! -d "/Applications/SwiftBar.app" ]; then
  echo "→ SwiftBar não encontrado. Instalando via Homebrew…"
  brew install --cask swiftbar
fi

# 2. Apontar o SwiftBar pra pasta plugins/ (só o plugin mora lá — evita o "?")
defaults write com.ameba.SwiftBar PluginDirectory "$PLUGIN_DIR"

# 3. Garantir que o plugin é executável (e limpar .pyc: SwiftBar tentaria rodar
#    qualquer arquivo em plugins/, e um __pycache__/*.pyc vira um "?" no menu)
rm -rf "$PLUGIN_DIR"/__pycache__
chmod +x "$PLUGIN_DIR"/crabbar.*.py

# 4. Abrir automaticamente no login (idempotente — não duplica)
osascript <<'EOF'
tell application "System Events"
  if not (exists login item "SwiftBar") then
    make login item at end with properties {path:"/Applications/SwiftBar.app", hidden:false}
  end if
end tell
EOF
echo "→ SwiftBar registrado nos itens de início (abre sozinho ao ligar o Mac)."

# 5. (Re)abrir agora
killall SwiftBar 2>/dev/null || true
sleep 1
open -a SwiftBar
echo "✓ Pronto. O contador 🦀 deve aparecer na barra, perto do relógio."
