# telegram-monitor

Monitor pessoal de promoções no Telegram. Ele lê os grupos e canais em que **sua
conta** participa e avisa você por um bot quando aparece uma mensagem que casa
com suas regras (ex: *Galaxy S25/S26 Plus ou Ultra até R$ 5.500, sem capa nem
película*).

- **Só leitura:** o userbot (Telethon) nunca envia, encaminha, edita nem apaga nada.
- **Regras** com termos de inclusão e exclusão (texto ou regex; sem acento e sem
  diferenciar maiúsculas), preço máximo opcional e liga/desliga.
- **Extração de preço** que ignora parcelas, cupons, frete e cashback e prioriza
  "com cupom", "no Pix", "à vista" e "por".
- **Deduplicação:** o mesmo post nunca é alertado duas vezes, e reposts da mesma
  promoção em outros grupos são suprimidos dentro de uma janela configurável.
- **Tela web** (HTML + JS puro) para escolher grupos, gerenciar regras, testar
  regras contra um texto colado e ver o histórico.
- **Resiliente:** reconecta sozinho, tem watchdog, avisa pelo bot se cair
  repetidamente e aceita um heartbeat externo opcional (healthchecks.io).
- **Leve:** cerca de 80 a 120 MB de RAM, feito para a VM Oracle Always Free
  E2.1.Micro (1 GB).

> ⚠️ Usar a API do Telegram com uma conta de usuário (userbot) é permitido, mas
> abuso pode levar a limitações na conta. Este projeto só lê mensagens de chats
> em que você já está, o que é o uso de menor risco. Use por sua conta e risco.

---

## Sumário

1. [Como funciona](#como-funciona)
2. [Segurança](#segurança)
3. [Passo 1: credenciais em my.telegram.org](#passo-1-credenciais-em-mytelegramorg)
4. [Passo 2: bot no BotFather](#passo-2-bot-no-botfather)
5. [Passo 3: VM Oracle Always Free](#passo-3-vm-oracle-always-free)
6. [Passo 4: instalação na VM](#passo-4-instalação-na-vm)
7. [Passo 5: primeiro login](#passo-5-primeiro-login)
8. [Passo 6: acessar a tela via túnel SSH](#passo-6-acessar-a-tela-via-túnel-ssh)
9. [Heartbeat externo (healthchecks.io)](#heartbeat-externo-healthchecksio)
10. [Escrevendo regras](#escrevendo-regras)
11. [Comandos do bot](#comandos-do-bot)
12. [Operação: logs, atualização, backup](#operação-logs-atualização-backup)
13. [Desenvolvimento local](#desenvolvimento-local)
14. [Solução de problemas](#solução-de-problemas)

---

## Como funciona

```
┌────────────────────────── processo único (systemd) ───────────────────────────┐
│  Telethon (sua conta, só leitura)                                             │
│     NewMessage / MessageEdited ──► Pipeline ─────────────► Bot (Bot API)      │
│                                     │ chat monitorado?        só para você    │
│                                     │ idade (novas ≤3h, edições ≤24h)         │
│                                     │ regras + preço                          │
│                                     │ dedup (post, texto, URL+preço,          │
│                                     │        regras+preço)                    │
│                                     ▼                                         │
│                                  SQLite  ◄── FastAPI em 127.0.0.1:8000        │
│  Supervisor: reconexão c/ backoff, watchdog, avisos | Heartbeat opcional      │
└───────────────────────────────────────────────────────────────────────────────┘
```

Estrutura do código:

```
src/tgmonitor/
├── __main__.py          CLI: login | run | check-config | gen-token
├── config.py            .env (pydantic-settings)
├── log.py               logging sem conteúdo de mensagens; mascara tokens/URLs
├── db.py, repository.py SQLite (schema, migrações, queries)
├── matching/            normalize.py, price.py, matcher.py
├── dedup.py             fingerprints
├── pipeline.py          fluxo de uma mensagem
├── telegram/            client.py (fachada só-leitura), links.py
├── notifier.py          bot: alertas + comandos do dono
├── supervisor.py        reconexão, watchdog, avisos
├── heartbeat.py         GET opcional em HEALTHCHECK_URL
├── service.py           orquestra tudo
└── web/                 api.py + static/ (index.html, app.js, style.css)
```

## Segurança

| Aspecto | Como é garantido |
|---|---|
| Credenciais | Só no `.env` (em produção, `/etc/tgmonitor/tgmonitor.env`, `640 root:tgmonitor`). Nada no repositório. |
| Sessão e banco | Em `DATA_DIR`, fora do repositório: diretório `700`, arquivos `600`, `umask 077`. Cobertos pelo `.gitignore`. |
| Login da conta | **Só** pelo comando interativo `tgmonitor login`. Não existe rota web de login. |
| Rede | A interface escuta **apenas** em `127.0.0.1` (fixo no código). O acesso é por túnel SSH, sem porta aberta na VM. |
| DNS rebinding | `TrustedHostMiddleware` aceita só `127.0.0.1`/`localhost`. |
| API | Todas as rotas `/api/*` exigem o header `X-API-Token` (`API_TOKEN` do `.env`, com 32+ caracteres). O frontend guarda o token só em memória. Sem CORS, sem `/docs` e com CSP restritiva. |
| Bot | Só responde ao `OWNER_USER_ID` em chat privado. Qualquer outra pessoa é ignorada em silêncio. |
| Privacidade | Nenhum conteúdo de mensagem vai para serviços externos. Os logs têm só ids e contadores. O heartbeat é um GET vazio. |
| Só leitura | O `TelegramClient` fica encapsulado em `ReadOnlyTelegram`, e os testes (`tests/test_readonly.py`) falham se a fachada expuser métodos de escrita ou se outro módulo usar o cliente. |
| Serviço | O systemd roda o processo com usuário dedicado, sem privilégios, com `ProtectSystem=strict`, `NoNewPrivileges`, `MemoryMax=300M` e outras proteções. |

---

## Passo 1: credenciais em my.telegram.org

1. Acesse <https://my.telegram.org> e entre com o **seu** número (o código chega no app do Telegram).
2. Clique em **API development tools**.
3. Preencha **App title** e **Short name** com qualquer valor (ex: `tgmonitor`). Em **Platform**, escolha *Other*.
4. Anote o **App api_id** (`TG_API_ID`) e o **App api_hash** (`TG_API_HASH`).

> Trate o `api_hash` como senha. Não o publique nem o compartilhe.

## Passo 2: bot no BotFather

1. No Telegram, abra [@BotFather](https://t.me/BotFather) e envie `/newbot`.
2. Escolha um nome e um username (terminado em `bot`).
3. Anote o **token** (`BOT_TOKEN`), algo como `123456789:AA...`.
4. **Abra o seu bot e envie `/start`.** Sem isso, o bot não consegue mandar mensagens para você.
5. Descubra seu **user_id** numérico (`OWNER_USER_ID`): envie qualquer mensagem para
   [@userinfobot](https://t.me/userinfobot), que responde com o seu `Id`.

Opcional (recomendado): no BotFather, use `/setjoingroups` → **Disable**. Assim o bot
não pode ser adicionado a grupos.

## Passo 3: VM Oracle Always Free

1. Crie uma conta em <https://cloud.oracle.com> (o plano Always Free exige cartão só para verificação).
2. Vá em **Compute → Instances → Create instance**:
   - **Image:** Canonical **Ubuntu 24.04** (necessário para o Python 3.12).
   - **Shape:** *Change shape* → *Specialty and previous generation* → **VM.Standard.E2.1.Micro** (Always Free).
   - **Networking:** mantenha a VCN padrão com IP público.
   - **SSH keys:** envie sua chave pública (`~/.ssh/id_ed25519.pub`) ou baixe a gerada pela Oracle.
3. **Não abra portas extras.** A Security List padrão libera só a 22 (SSH), e é só dela que você precisa.
4. Conecte:

   ```bash
   ssh ubuntu@IP_DA_VM
   sudo apt update && sudo apt -y upgrade
   ```

## Passo 4: instalação na VM

```bash
sudo git clone https://github.com/SEU_USUARIO/telegram-monitor.git /opt/telegram-monitor
sudo bash /opt/telegram-monitor/deploy/setup-vm.sh
```

O script faz o seguinte (e pode ser rodado de novo sem problema):

- instala Python e git;
- cria **2 GB de swap**;
- cria o usuário `tgmonitor`, `/var/lib/telegram-monitor` (700) e a venv em `/opt/telegram-monitor/.venv`;
- cria `/etc/tgmonitor/tgmonitor.env` a partir do `.env.example`, **já com um `API_TOKEN` aleatório**;
- instala e habilita o serviço systemd, sem iniciá-lo;
- instala o atalho `tgmonitor-admin`, que roda a CLI como o usuário do serviço.

Preencha as credenciais:

```bash
sudo nano /etc/tgmonitor/tgmonitor.env      # TG_API_ID, TG_API_HASH, BOT_TOKEN, OWNER_USER_ID
sudo tgmonitor-admin check-config
```

## Passo 5: primeiro login

O login é **sempre** feito pela linha de comando, com o serviço **parado**:

```bash
sudo systemctl stop telegram-monitor
sudo tgmonitor-admin login
```

Informe o telefone no formato internacional (`+5511...`), o código que chega no app
do Telegram e, se tiver, a senha de verificação em duas etapas. A sessão fica salva em
`/var/lib/telegram-monitor/account.session`, com permissão 600.

Depois inicie o serviço:

```bash
sudo systemctl start telegram-monitor
journalctl -u telegram-monitor -f
```

Na primeira execução, a lista de grupos e canais é carregada automaticamente.

> A sessão equivale a estar logado na sua conta. Se desconfiar de vazamento, encerre-a
> em *Telegram → Configurações → Dispositivos*.

## Passo 6: acessar a tela via túnel SSH

A interface **nunca** fica exposta na internet. No **seu computador** (Linux, macOS ou
Windows 10+, pelo PowerShell com OpenSSH):

```bash
ssh -N -L 8000:127.0.0.1:8000 ubuntu@IP_DA_VM
```

Deixe esse terminal aberto e acesse **<http://localhost:8000>**. A tela pede o
`API_TOKEN`, que você consulta na VM:

```bash
sudo grep API_TOKEN /etc/tgmonitor/tgmonitor.env
```

O token fica só na memória da aba e é pedido de novo ao recarregar a página.

Na tela:

1. **Grupos:** marque os grupos e canais a monitorar. Use "Atualizar lista" depois de entrar em grupos novos.
2. **Regras:** crie as regras. Dá para testar o rascunho antes de salvar.
3. **Testar:** cole o texto de uma promoção e veja quais regras casam, os termos que bateram e o preço extraído.
4. **Histórico:** últimos 100 alertas.
5. **Ajustes:** janela de dedup, idade máxima das mensagens, retenção, pausa e saúde do serviço.

## Heartbeat externo (healthchecks.io)

O serviço avisa pelo bot quando o listener cai repetidamente. Mas se a **VM inteira**
parar, ninguém avisa. Para isso existe o heartbeat opcional:

1. Crie uma conta gratuita em <https://healthchecks.io> e um novo *Check*.
2. Configure **Period: 5 minutes** e **Grace: 10 minutes**.
3. Em *Integrations*, ligue a notificação que preferir (e-mail, Telegram etc.).
4. Copie a *ping URL* (`https://hc-ping.com/<uuid>`) para o `.env`:

   ```bash
   HEALTHCHECK_URL=https://hc-ping.com/<uuid>
   ```

5. `sudo systemctl restart telegram-monitor`

O serviço faz um `GET` vazio nessa URL a cada 5 minutos, **só enquanto o listener está
saudável**. Se a VM cair, ou o listener travar, os pings param e o healthchecks.io avisa.
Nenhum dado é enviado.

---

## Escrevendo regras

Cada regra tem **termos de inclusão** (basta um casar), **termos de exclusão** (qualquer
um descarta), **preço máximo** opcional e um liga/desliga.

**Termos simples** casam por palavra inteira, sem acento e sem diferenciar maiúsculas:

| Termo | Casa com | Não casa com |
|---|---|---|
| `fe` | "S25 **FE**" | "per**fe**ito", "ca**fé**" |
| `película` | "**PELICULA**", "**Película**" | |
| `s25 ultra` | "**S25   Ultra**", "**s25**↵**ultra**" | "S25Ultra" |
| `s25+` | "Galaxy **S25+**" | "S255" |

**Regex** vão entre barras e são aplicadas ao texto já normalizado (minúsculo, sem acento):

```
/s2[56]\s*(\+|plus|ultra)/
```

Esse padrão casa com S25+, S25 Plus, S25 Ultra, S26+, S26 Plus e S26 Ultra.

Exemplo completo:

| Campo | Valor |
|---|---|
| Nome | Galaxy S25/S26 Plus/Ultra |
| Inclusão | `/s2[56]\s*(\+\|plus\|ultra)/` |
| Exclusão | `capa` · `capinha` · `película` · `fe` · `usado` · `seminovo` · `vitrine` |
| Preço máximo | 5.500,00 |

**Princípio: na dúvida, alertar.** Exclusões e preço máximo são opcionais e, quando
vazios, não descartam nada. Se a regra tem preço máximo mas a mensagem não tem preço
identificável, o alerta vai mesmo assim, marcado com **"⚠️ preço não identificado"**.

### Como o preço é extraído

Todos os valores `R$ ...` são classificados pelo contexto:

| Prioridade | Tipo | Exemplos |
|---|---|---|
| 1 | Preço final | `com cupom: R$ 5.299`, `com desconto R$ 5.199`, `no Pix R$ 5.199`, `à vista R$ 5.199`, `R$ 5.199 no Pix` |
| 2 | "por" | `por R$ 5.499`, `agora R$ 5.499` |
| 3 | Neutro | qualquer outro |
| — | Ignorado | `12x de R$ 458`, `cupom de R$ 200`, `R$ 200 OFF`, `desconto de R$ 300`, `R$ 300 de desconto`, `frete R$ 19,90`, `cashback de R$ 100` |

O preço usado é o **menor valor da maior prioridade presente**. Por exemplo, em
`de R$ 5.999 por R$ 5.499, com cupom: R$ 5.299` o preço é **R$ 5.299**. A aba
**Testar** mostra a classificação de cada valor encontrado.

### Deduplicação

- O **mesmo post** (chat + id) nunca gera dois alertas, nem quando é editado.
- Dentro da janela (padrão de 6h), um alerta é suprimido se bater com um anterior em:
  - **texto** normalizado (sem emojis, pontuação e links);
  - **URL + preço** (URL sem `utm_*`, `fbclid` etc.). O mesmo link com **preço menor gera alerta novo**;
  - **regras casadas + preço**, que pega a mesma promoção repostada com links de afiliado diferentes.
- As **edições** são monitoradas: um post editado para incluir o produto gera alerta,
  desde que a mensagem tenha menos de 24h.
- As mensagens com **mais de 3h** são ignoradas. Isso evita alertar promoções vencidas
  que chegam em lote quando o serviço reconecta.

Todos esses limites podem ser ajustados em **Ajustes**.

## Comandos do bot

| Comando | Ação |
|---|---|
| `/status` | Estado do listener, regras, contadores e uptime |
| `/pause` | Pausa os alertas (o monitor continua conectado) |
| `/resume` | Retoma os alertas |
| `/help` | Lista os comandos |

Comandos de qualquer outra pessoa são ignorados. Comandos com mais de 5 minutos,
enfileirados enquanto o serviço estava fora, também.

## Operação: logs, atualização, backup

```bash
# Estado e logs
systemctl status telegram-monitor
journalctl -u telegram-monitor -f
journalctl -u telegram-monitor --since "1 hour ago"

# Atualizar
cd /opt/telegram-monitor && sudo git pull
sudo /opt/telegram-monitor/.venv/bin/pip install -q /opt/telegram-monitor
sudo systemctl restart telegram-monitor

# Backup (sessão + banco). Guarde em local seguro: a sessão dá acesso à sua conta!
sudo tar czf ~/tgmonitor-backup.tgz -C /var/lib telegram-monitor /etc/tgmonitor
chmod 600 ~/tgmonitor-backup.tgz

# Memória
systemctl show telegram-monitor -p MemoryCurrent
```

**Códigos de saída:** `2` significa sessão do Telegram inválida. Nesse caso o systemd
**não** reinicia o serviço (`RestartPreventExitStatus=2`) e o bot manda um aviso. Rode
`sudo tgmonitor-admin login` e depois `sudo systemctl start telegram-monitor`.

**Desinstalar:**

```bash
sudo systemctl disable --now telegram-monitor
sudo rm -rf /etc/systemd/system/telegram-monitor.service /etc/tgmonitor \
  /var/lib/telegram-monitor /opt/telegram-monitor /usr/local/bin/tgmonitor-admin
sudo userdel tgmonitor
```

---

## Desenvolvimento local

Requer Python 3.12+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

pytest -q                 # testes
ruff check src tests      # lint
ruff format src tests     # formatação
mypy src                  # tipos (strict)
```

Para rodar localmente:

```bash
cp .env.example .env && chmod 600 .env    # preencha
tgmonitor gen-token                       # cole em API_TOKEN
tgmonitor check-config
tgmonitor login
tgmonitor run                             # http://127.0.0.1:8000
```

Por padrão, os dados ficam em `~/.local/share/telegram-monitor`.

## Solução de problemas

| Sintoma | Causa provável / solução |
|---|---|
| `bot ERRO: Unauthorized` no check-config | `BOT_TOKEN` errado. |
| Alertas não chegam, log mostra `status=403` | Você não enviou `/start` para o seu bot. |
| `database is locked` no login | O serviço está rodando. Pare-o antes de `tgmonitor-admin login`. |
| Serviço parado com `status=2` | Sessão revogada ou expirada. Refaça o login (Passo 5). |
| Tela responde `400 Invalid host header` | Acesse por `http://localhost:8000` ou `http://127.0.0.1:8000`, não por outro nome. |
| Grupo novo não aparece | Clique em **Atualizar lista** na aba Grupos. |
| Link "Abrir mensagem" ausente | Grupos antigos do tipo "grupo básico" não têm link de mensagem no Telegram. |

## Licença

[MIT](LICENSE)
