#!/usr/bin/env bash
# Setup do tgmonitor numa VM Ubuntu 24.04 (ex: Oracle Always Free E2.1.Micro, 1 GB).
# Idempotente: pode rodar de novo para atualizar dependências/unit.
#
# Uso (como root, com o repositório já clonado em /opt/telegram-monitor):
#   sudo git clone https://github.com/SEU_USUARIO/telegram-monitor.git /opt/telegram-monitor
#   sudo bash /opt/telegram-monitor/deploy/setup-vm.sh
set -euo pipefail

APP_DIR=/opt/telegram-monitor
DATA_DIR=/var/lib/telegram-monitor
CONF_DIR=/etc/tgmonitor
ENV_FILE=$CONF_DIR/tgmonitor.env
SVC_USER=tgmonitor
SWAP_FILE=/swapfile
SWAP_SIZE=2G

log() { printf '\n==> %s\n' "$*"; }

[[ $EUID -eq 0 ]] || { echo "Rode como root (sudo)." >&2; exit 1; }
[[ -f $APP_DIR/pyproject.toml ]] || { echo "Clone o repositório em $APP_DIR primeiro." >&2; exit 1; }

log "Pacotes do sistema"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip git

PYV=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
if [[ $(printf '%s\n' "3.12" "$PYV" | sort -V | head -1) != "3.12" ]]; then
  echo "Python $PYV encontrado; é necessário 3.12+. Use Ubuntu 24.04." >&2
  exit 1
fi

log "Swap ($SWAP_SIZE) — evita OOM na VM de 1 GB"
if ! swapon --show | grep -q .; then
  fallocate -l "$SWAP_SIZE" "$SWAP_FILE"
  chmod 600 "$SWAP_FILE"
  mkswap "$SWAP_FILE" >/dev/null
  swapon "$SWAP_FILE"
  grep -q "^$SWAP_FILE " /etc/fstab || echo "$SWAP_FILE none swap sw 0 0" >> /etc/fstab
  sysctl -q vm.swappiness=10
  echo "vm.swappiness=10" > /etc/sysctl.d/99-tgmonitor-swap.conf
else
  echo "swap já configurado"
fi

log "Usuário de serviço '$SVC_USER'"
id "$SVC_USER" &>/dev/null || useradd --system --home-dir /nonexistent --no-create-home \
  --shell /usr/sbin/nologin "$SVC_USER"

log "Diretórios"
install -d -m 700 -o "$SVC_USER" -g "$SVC_USER" "$DATA_DIR"
install -d -m 750 -o root -g "$SVC_USER" "$CONF_DIR"

log "Ambiente Python em $APP_DIR/.venv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q "$APP_DIR"

log "Arquivo de configuração $ENV_FILE"
if [[ ! -f $ENV_FILE ]]; then
  TOKEN=$("$APP_DIR/.venv/bin/tgmonitor" gen-token)
  sed -e "s|^API_TOKEN=.*|API_TOKEN=$TOKEN|" \
      -e "s|^# DATA_DIR=/var/lib/telegram-monitor|DATA_DIR=$DATA_DIR|" \
      "$APP_DIR/.env.example" > "$ENV_FILE"
  echo "criado (API_TOKEN gerado automaticamente). EDITE para preencher as credenciais."
else
  echo "já existe; mantido"
fi
chown root:"$SVC_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

log "Comando administrativo /usr/local/bin/tgmonitor-admin"
cat > /usr/local/bin/tgmonitor-admin <<EOF
#!/bin/sh
# Roda a CLI do tgmonitor como o usuário de serviço, com a configuração de produção.
cd $DATA_DIR || exit 1
exec sudo -u $SVC_USER $APP_DIR/.venv/bin/tgmonitor --env-file $ENV_FILE "\$@"
EOF
chmod 755 /usr/local/bin/tgmonitor-admin

log "Serviço systemd"
install -m 644 "$APP_DIR/deploy/telegram-monitor.service" /etc/systemd/system/telegram-monitor.service
systemctl daemon-reload
systemctl enable telegram-monitor.service >/dev/null

cat <<EOF

Pronto. Próximos passos:
  1. sudo nano $ENV_FILE             # preencha TG_API_ID, TG_API_HASH, BOT_TOKEN, OWNER_USER_ID
  2. sudo tgmonitor-admin check-config
  3. sudo systemctl stop telegram-monitor && sudo tgmonitor-admin login
  4. sudo systemctl start telegram-monitor && journalctl -u telegram-monitor -f
  5. No seu computador: ssh -N -L 8000:127.0.0.1:8000 ubuntu@IP_DA_VM
     e abra http://localhost:8000  (API_TOKEN: sudo grep API_TOKEN $ENV_FILE)
EOF
