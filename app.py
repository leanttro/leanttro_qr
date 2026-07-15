from flask import Flask, render_template, request, redirect, session, flash, url_for, abort, send_file, jsonify
import psycopg2
import psycopg2.extras
import requests
import os
import io
import json
import re
import unicodedata
import secrets
import smtplib
import base64
from urllib.parse import quote
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
import qrcode

load_dotenv()


def _env_obrigatoria(nome):
    """Lê uma variável de ambiente obrigatória. Derruba o app na subida se faltar,
    em vez de cair silenciosamente em uma credencial hardcoded no código."""
    valor = os.getenv(nome)
    if not valor:
        raise RuntimeError(
            f"Variável de ambiente obrigatória '{nome}' não foi definida. "
            f"Configure-a no .env (local) ou nas envs do Dokploy (produção)."
        )
    return valor


app = Flask(__name__)
app.secret_key = _env_obrigatoria("SECRET_KEY")

# --- SUBDOMÍNIO (fidelize.qrcodebrindes.com.br) ---
# SERVER_NAME só existe se a env SERVER_NAME estiver setada. Sem isso, o
# Werkzeug não tem como calcular subdomínio a partir do Host (é assim que o
# Flask decide se uma rota com subdomain='fidelize' bate ou não) — mas setar
# fixo no código quebraria o `app.run()` local, onde o Host é localhost:5002.
# Em produção (Dokploy), defina SERVER_NAME=qrcodebrindes.com.br no .env.
app.config['SERVER_NAME'] = os.getenv('SERVER_NAME')

# Desde o Flask 2.3, SESSION_COOKIE_DOMAIN deixou de ser derivado
# automaticamente do SERVER_NAME — precisa ser setado explícito. Sem isso, o
# cookie de sessão fica preso ao host exato onde foi criado: alguém logado em
# fidelize.qrcodebrindes.com.br (fidelize_conta_id) NÃO leva a sessão pro
# domínio apex (qrcodebrindes.com.br), e rotas como /<slug>/painel (que
# vivem no apex e checam fidelize_conta_id via pode_gerenciar_pagina) nunca
# reconhecem esse login — é exatamente o loop de redirecionamento pro /login
# que estava acontecendo. O "." na frente faz o cookie valer tanto pro apex
# quanto pra qualquer subdomínio (fidelize.*, e futuros).
if app.config['SERVER_NAME']:
    app.config['SESSION_COOKIE_DOMAIN'] = f".{app.config['SERVER_NAME']}"

# --- CONFIGURAÇÕES ---
BASE_URL = os.getenv("BASE_URL", "https://qrcodebrindes.com")
FIDELIZE_BASE_URL = os.getenv("FIDELIZE_BASE_URL", "https://fidelize.qrcodebrindes.com.br")

DB_CONFIG = {
    "host": _env_obrigatoria("DB_HOST"),
    "port": int(os.getenv("DB_PORT", 5432)),
    "dbname": _env_obrigatoria("DB_NAME"),
    "user": _env_obrigatoria("DB_USER"),
    "password": _env_obrigatoria("DB_PASSWORD"),
}

IMGUR_CLIENT_ID = os.getenv("IMGUR_CLIENT_ID", "")

TAXA_LEAD_PADRAO = float(os.getenv("TAXA_LEAD_PADRAO", 20.00))

# --- E-MAIL (opcional — usado pra enviar senha resetada pro usuário) ---
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)

# Link fixo de pagamento pra templates pagos — mantido como fallback caso o
# Pix dinâmico abaixo não esteja configurado (PIX_CHAVE vazia).
LINK_PAGAMENTO_TEMPLATES = os.getenv("LINK_PAGAMENTO_TEMPLATES", "")

# --- PIX (QR dinâmico pra templates pagos, sem gateway/taxa) ---
PIX_CHAVE = os.getenv("PIX_CHAVE", "")
PIX_NOME_RECEBEDOR = os.getenv("PIX_NOME_RECEBEDOR", "")
PIX_CIDADE = os.getenv("PIX_CIDADE", "")
WHATSAPP_COMPROVANTE = os.getenv("WHATSAPP_COMPROVANTE", "")

# Quantas horas um slug fica "reservado" pra quem escolheu template pago mas
# ainda não pagou. Depois disso, o slug é liberado de novo pra qualquer um.
PIX_RESERVA_HORAS = 48

# --- FIDELIZE — trial e mensalidade ---
# Quantos QR codes toda conta nova ganha de graça no cadastro, sem precisar
# de pagamento nenhum (ver fidelize_cadastro). Ela pode usar e testar na
# hora; o Pix só entra quando esse saldo acabar.
FIDELIZE_QR_CODES_TRIAL = 3
# Teto de QR codes por conta, mesmo pra quem já paga mensalidade (ver
# fidelize_criar_qr) — sem isso, mensalidade_ativa=True liberaria criação
# ilimitada.
FIDELIZE_LIMITE_QR_CODES_POR_CONTA = 100
# Valor da mensalidade que libera criação até o teto acima. Planos maiores
# (mais de 100 QR codes) ainda não têm preço fechado — continuam sendo
# negociados na mão pelo WhatsApp, sem Pix automático.
FIDELIZE_PRECO_MENSALIDADE = 19.00
# Número pra onde o botão de "mandar comprovante" do Fidelize aponta. Cai no
# mesmo WhatsApp do QRCodeBrindes por padrão; dá pra separar depois com uma
# env própria (WHATSAPP_FIDELIZE) se você quiser atender em número diferente.
WHATSAPP_FIDELIZE = os.getenv("WHATSAPP_FIDELIZE", WHATSAPP_COMPROVANTE)

# --- BANCO ---
def get_db():
    conn = psycopg2.connect(**DB_CONFIG)
    return conn


def query_one(sql, params=None):
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        row = cur.fetchone()
        cur.close()
        return dict(row) if row else None
    finally:
        conn.close()


def query_all(sql, params=None):
    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql, params or ())
        rows = cur.fetchall()
        cur.close()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def execute(sql, params=None):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        conn.commit()
        cur.close()
    finally:
        conn.close()


def execute_returning(sql, params=None):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute(sql, params or ())
        result = cur.fetchone()
        conn.commit()
        cur.close()
        return result[0] if result else None
    finally:
        conn.close()


# --- PIX ESTÁTICO COM VALOR (BR Code / EMV, padrão Banco Central) ---
# Não depende de gateway nenhum — é só o formato padrão que qualquer app de
# banco lê. Isso monta o "copia e cola" (e a partir dele o QR); a lógica
# segue a especificação EMV: cada campo é ID (2 dígitos) + tamanho (2
# dígitos) + valor, e o payload inteiro fecha com um CRC16.
def _pix_tlv(id_campo, valor):
    return f"{id_campo}{len(valor):02d}{valor}"


def _pix_crc16(payload):
    """CRC16-CCITT (poly 0x1021, init 0xFFFF) — é o checksum exigido no
    campo final (63) do BR Code."""
    poly = 0x1021
    crc = 0xFFFF
    for byte in payload.encode('utf-8'):
        crc ^= (byte << 8)
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ poly) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return format(crc, '04X')


def gerar_pix_payload(chave, nome, cidade, valor, txid):
    """Monta o payload Pix estático com valor fixo (copia e cola / QR).
    nome: até 25 caracteres, sem acento. cidade: até 15, sem acento.
    txid: identificador da cobrança, só alfanumérico, até 25 caracteres."""
    txid_limpo = re.sub(r'[^A-Za-z0-9]', '', txid or '')[:25] or 'QRCODEBRINDES'

    conta = _pix_tlv('00', 'br.gov.bcb.pix') + _pix_tlv('01', chave)
    dados_adicionais = _pix_tlv('05', txid_limpo)

    payload_sem_crc = (
        _pix_tlv('00', '01') +                       # Payload Format Indicator
        _pix_tlv('01', '12') +                        # Point of Initiation (12 = uso único)
        _pix_tlv('26', conta) +                        # Merchant Account Info (Pix)
        _pix_tlv('52', '0000') +                       # Merchant Category Code
        _pix_tlv('53', '986') +                        # Moeda (986 = BRL)
        _pix_tlv('54', f'{valor:.2f}') +                # Valor da cobrança
        _pix_tlv('58', 'BR') +                          # País
        _pix_tlv('59', nome[:25]) +                     # Nome do recebedor
        _pix_tlv('60', cidade[:15]) +                   # Cidade do recebedor
        _pix_tlv('62', dados_adicionais) +              # TXID
        '6304'                                          # Abre o campo do CRC (ID+tamanho)
    )
    return payload_sem_crc + _pix_crc16(payload_sem_crc)


def gerar_pix_qr_base64(payload):
    """Gera o PNG do QR a partir do payload Pix e devolve já em base64,
    pronto pra jogar num <img src="data:image/png;base64,...">."""
    img = qrcode.make(payload)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode('ascii')


def slug_disponivel(slug):
    """Verifica se um slug pode ser usado agora. Diferente de
    get_pagina_by_slug (que só enxerga ativas — usada nas rotas públicas de
    acesso), essa função TAMBÉM considera páginas 'pendente' de pagamento,
    pra duas pessoas não reservarem o mesmo slug ao mesmo tempo. Uma
    pendente com mais de PIX_RESERVA_HORAS é tratada como expirada e libera
    o slug de novo, sem precisar de job/cron separado."""
    existente = query_one(
        "SELECT plano, ativo, created_at FROM brindes_paginas WHERE slug = %s",
        (slug,)
    )
    if not existente:
        return True
    if existente['ativo']:
        return False
    if existente['plano'] == 'pendente' and existente['created_at']:
        limite = datetime.now() - timedelta(hours=PIX_RESERVA_HORAS)
        if existente['created_at'].replace(tzinfo=None) >= limite:
            return False  # ainda dentro do prazo de reserva
    return True


def rodar_auto_migracoes():
    """Roda uma vez quando o app sobe. Cria a coluna campos_extra em
    brindes_paginas se ela ainda não existir — assim ninguém precisa entrar
    no banco na mão pra usar campos extras de template (ex: Cartão
    Fidelidade). ADD COLUMN IF NOT EXISTS é seguro rodar toda vez que o app
    reinicia: se a coluna já existe, não faz nada."""
    try:
        execute("""
            ALTER TABLE brindes_paginas
            ADD COLUMN IF NOT EXISTS campos_extra JSONB DEFAULT '{}'::jsonb
        """)
        print("Auto-migração ok: coluna campos_extra confirmada em brindes_paginas.")
    except Exception as e:
        print(f"Aviso: auto-migração de campos_extra falhou ({e}). "
              f"Salvar campos extras de template pode não funcionar até isso ser corrigido.")

    try:
        execute("""
            ALTER TABLE brindes_paginas
            ADD COLUMN IF NOT EXISTS apelido_slug TEXT
        """)
        print("Auto-migração ok: coluna apelido_slug confirmada em brindes_paginas.")
    except Exception as e:
        print(f"Aviso: auto-migração de apelido_slug falhou ({e}). "
              f"Edição de link do Fidelize pode não funcionar até isso ser corrigido.")

    try:
        execute("""
            ALTER TABLE brindes_fidelidade_contas
            ADD COLUMN IF NOT EXISTS templates_extras JSONB DEFAULT '[]'::jsonb
        """)
        print("Auto-migração ok: coluna templates_extras confirmada em brindes_fidelidade_contas.")
    except Exception as e:
        print(f"Aviso: auto-migração de templates_extras falhou ({e}). "
              f"Templates exclusivos do Fidelize podem não funcionar até isso ser corrigido.")


rodar_auto_migracoes()


def sync_brinde_tipos_impressao(brinde_id, tipo_impressao_ids):
    """Substitui o conjunto de tipos de impressão de um brinde em
    brindes_brinde_tipos_impressao (M2M). Apaga as relações antigas daquele
    brinde e insere as novas, tudo numa transação só."""
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM brindes_brinde_tipos_impressao WHERE brinde_id = %s", (brinde_id,))
        for tipo_id in tipo_impressao_ids:
            cur.execute(
                "INSERT INTO brindes_brinde_tipos_impressao (brinde_id, tipo_impressao_id) VALUES (%s, %s)",
                (brinde_id, tipo_id)
            )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def get_tipos_impressao_ids(brinde_id):
    rows = query_all(
        "SELECT tipo_impressao_id FROM brindes_brinde_tipos_impressao WHERE brinde_id = %s",
        (brinde_id,)
    )
    return [r['tipo_impressao_id'] for r in rows]


# --- RATE LIMIT ARTESANAL (mesmo padrão do SOS Motoboy) ---
request_log = {}

def check_limit(key, limit, period_seconds):
    now = datetime.now()
    if key not in request_log:
        request_log[key] = []
    request_log[key] = [t for t in request_log[key] if t > now - timedelta(seconds=period_seconds)]
    if len(request_log[key]) >= limit:
        return False
    request_log[key].append(now)
    return True


def get_ip():
    if request.headers.getlist("X-Forwarded-For"):
        return request.headers.getlist("X-Forwarded-For")[0]
    return request.remote_addr


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('admin_id'):
            return redirect('/admin/login')
        return f(*args, **kwargs)
    return decorated


# --- REDE DE SEGURANÇA PRO SERVER_NAME ---
@app.before_request
def redireciona_www_para_apex():
    """Com SERVER_NAME fixo (necessário pro subdomínio fidelize funcionar),
    um Host 'www.qrcodebrindes.com.br' NÃO bate com nenhuma rota — o Werkzeug
    trataria 'www' como se fosse só mais um subdomínio desconhecido, e tudo
    devolveria 404. Isso só é um problema se o Traefik alguma hora mandar
    tráfego de www direto pro container; se o Traefik já redireciona www pra
    apex antes de chegar aqui, este bloco nunca dispara — é só um seguro.
    Só entra em ação se SERVER_NAME estiver configurado (ou seja, em produção)."""
    server_name = app.config.get('SERVER_NAME')
    if not server_name:
        return
    host = request.host
    host_sem_porta = host.split(':')[0].lower()
    if host_sem_porta == f"www.{server_name}".lower():
        apex_com_porta = host[4:]  # remove só o "www." do início, preserva porta se houver
        return redirect(f"{request.scheme}://{apex_com_porta}{request.full_path}", code=301)


# --- MIDDLEWARE ANTI-BOT (mesmo padrão do SOS Motoboy) ---
@app.before_request
def block_scrapers():
    user_agent = request.headers.get('User-Agent', '').lower()
    bots = ['python-requests', 'curl', 'wget', 'libwww-perl', 'scrapy', 'httpclient']
    if any(bot in user_agent for bot in bots):
        abort(403, description="Acesso negado.")


# --- HELPERS ---
def upload_imgur(file_storage):
    """Sobe uma imagem pro Imgur e devolve só a URL pública. Sem Directus."""
    try:
        url = "https://api.imgur.com/3/image"
        headers = {"Authorization": f"Client-ID {IMGUR_CLIENT_ID}"}
        files = {"image": file_storage.read()}
        response = requests.post(url, headers=headers, files=files)
        if response.status_code in (200, 201):
            return response.json()["data"]["link"]
    except Exception as e:
        print(f"Erro upload Imgur: {e}")
    return None


def gerar_senha_aleatoria(tamanho=10):
    """Gera uma senha aleatória fácil de digitar (letras minúsculas + dígitos, sem caracteres ambíguos)."""
    alfabeto = "abcdefghjkmnpqrstuvwxyz23456789"  # sem 0/o/1/l/i pra evitar confusão
    return "".join(secrets.choice(alfabeto) for _ in range(tamanho))


def slugify_cidade(texto):
    """Normaliza um nome de cidade pra slug: remove acento, minúsculo, espaço vira hífen."""
    if not texto:
        return None
    texto = unicodedata.normalize('NFKD', texto).encode('ascii', 'ignore').decode('ascii')
    texto = texto.strip().lower()
    texto = re.sub(r'[^a-z0-9]+', '-', texto).strip('-')
    return texto or None


# Validação só de FORMATO (não confirma que a caixa de e-mail existe de
# verdade — isso exigiria e-mail de confirmação ou sondagem SMTP, que não
# foram pedidos). Padrão simples e prático: algo@algo.algo, sem espaço.
EMAIL_REGEX = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def email_ja_usado_nesse_template(email, template_slug):
    """Verifica se esse e-mail já criou um QR code com ESSE MESMO template
    (fluxo /gerar-qr, catálogo geral). Mesmo e-mail com template diferente
    é permitido de propósito — é o jeito da pessoa testar templates
    diferentes sem trocar de e-mail. Só considera páginas ativas."""
    existente = query_one(
        "SELECT id FROM brindes_paginas WHERE email = %s AND template = %s AND ativo = TRUE",
        (email, template_slug)
    )
    return existente is not None


def carregar_templates():
    """Varre templates/paginas/ e devolve os metadados (<slug>.json) de cada
    template disponível (<slug>.html + <slug>.json lado a lado).

    A preview_url (URL da imagem de capa) é sobrescrita pelo valor salvo em
    brindes_templates_capas, se existir — porque o arquivo .json em disco
    não sobrevive a redeploy, mas o banco sim."""
    templates = []
    pasta = os.path.join(app.template_folder, 'paginas')
    if not os.path.isdir(pasta):
        return templates
    for arquivo in os.listdir(pasta):
        if arquivo.endswith('.html'):
            slug = arquivo[:-5]
            meta_path = os.path.join(pasta, f'{slug}.json')
            if os.path.exists(meta_path):
                with open(meta_path, encoding='utf-8') as f:
                    meta = json.load(f)
                meta['slug'] = slug
                meta['arquivo'] = f'paginas/{arquivo}'
                templates.append(meta)

    try:
        capas = query_all("SELECT slug, preview_url FROM brindes_templates_capas")
        capas_por_slug = {c['slug']: c['preview_url'] for c in capas}
        for meta in templates:
            url_salva = capas_por_slug.get(meta['slug'])
            if url_salva:
                meta['preview_url'] = url_salva
    except Exception:
        # Se a tabela ainda não existir ou o banco estiver fora do ar,
        # simplesmente segue com o preview_url que veio do .json.
        pass

    return templates


def get_template_por_slug(slug):
    return next((t for t in carregar_templates() if t['slug'] == slug), None)


def conta_pode_usar_template(conta, template_meta):
    """Um template só é restrito se o .json dele tiver "exclusivo": true.
    Templates sem essa marcação continuam liberados pra qualquer conta,
    exatamente como sempre foi. Pra um exclusivo, só libera se o slug dele
    estiver na lista templates_extras daquela conta (coluna JSONB, editada
    pelo admin em /admin/fidelize/<id>/liberar-template)."""
    if not template_meta:
        return False
    if not template_meta.get('exclusivo'):
        return True
    extras = conta.get('templates_extras') or []
    if isinstance(extras, str):
        try:
            extras = json.loads(extras)
        except (ValueError, TypeError):
            extras = []
    return template_meta['slug'] in extras


app.jinja_env.globals['get_template_por_slug'] = get_template_por_slug


def enviar_email(destinatario, assunto, corpo_texto):
    """Envia um e-mail simples via SMTP. Retorna (sucesso: bool, erro: str|None).
    Se SMTP não estiver configurado no .env, retorna sucesso=False com uma mensagem clara,
    sem derrubar o resto da aplicação."""
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASSWORD:
        return False, "SMTP não configurado no .env (SMTP_HOST/SMTP_USER/SMTP_PASSWORD)."

    try:
        msg = MIMEText(corpo_texto, "plain", "utf-8")
        msg["Subject"] = assunto
        msg["From"] = SMTP_FROM
        msg["To"] = destinatario

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.sendmail(SMTP_FROM, [destinatario], msg.as_string())
        return True, None
    except Exception as e:
        return False, str(e)


def registrar_scan(pagina_id):
    try:
        execute(
            "INSERT INTO brindes_scans (pagina_id, ip, user_agent) VALUES (%s, %s, %s)",
            (pagina_id, get_ip(), request.headers.get('User-Agent', ''))
        )
    except Exception as e:
        print(f"Erro ao registrar scan: {e}")


def get_pagina_by_slug(slug):
    return query_one("SELECT * FROM brindes_paginas WHERE slug = %s AND ativo = TRUE", (slug,))


def montar_page_context(pagina):
    """Mescla os campos extras dinâmicos do template (ex: 'meta', 'premio',
    'carimbos_atual' do Cartão Fidelidade) dentro do dict da página, pra
    templates poderem usar {{ page.premio }} normalmente. Também devolve
    carimbos_atual separado, já que o template usa essa variável solta.
    campos_extra é uma coluna JSONB — se a página ainda não tiver essa
    coluna (banco não migrado), simplesmente não mescla nada."""
    page = dict(pagina)
    extra = pagina.get('campos_extra') or {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except (ValueError, TypeError):
            extra = {}
    for chave, valor in extra.items():
        if chave not in page or page.get(chave) in (None, ''):
            page[chave] = valor
    carimbos_atual = extra.get('carimbos_atual', 0)
    try:
        carimbos_atual = int(carimbos_atual)
    except (ValueError, TypeError):
        carimbos_atual = 0
    return page, carimbos_atual


def gerar_timeline_events(pagina):
    """Placeholder: timeline vem de timeline_json (lista de {date, title})."""
    return pagina.get("timeline_json") or []


# --- ANÚNCIOS (multi-escopo: ocasião, tipo de impressão, cidade, funcionalidades) ---
def get_anuncio(posicao, ocasiao_id=None, tipo_impressao_id=None, cidade_slug=None, contexto='catalogo'):
    """Retorna 1 anúncio ativo pra posição informada ('topo' ou 'meio'), dentro
    da vigência (data_inicio/data_fim) e do escopo da página atual.

    Cada anúncio pode ter até 4 filtros de escopo, todos opcionais e
    combinados com E (AND) entre si:
      - ocasiao_id             -> só aparece na(s) página(s) daquela ocasião
      - tipo_impressao_id      -> só aparece na página daquele tipo de impressão
      - cidade_slug            -> só aparece na(s) página(s) daquela cidade
      - apenas_funcionalidades -> só aparece nas páginas de ferramenta do site
        (gerar-qr, painel, demo), nunca no catálogo público

    Um anúncio sem NENHUM escopo preenchido aparece em QUALQUER página
    (é o "todas as páginas" da UI do admin). Quando mais de um anúncio
    ativo bate com a página atual, o mais específico (mais escopos
    preenchidos) tem prioridade sobre o genérico.

    `contexto`:
      - 'catalogo'       -> home, ocasião, brinde, tipo de impressão,
        cidade, empresa, diretório. Nunca mostra anúncios marcados como
        apenas_funcionalidades.
      - 'funcionalidade' -> gerar-qr, painel, demo. Só mostra anúncios
        marcados como apenas_funcionalidades OU totalmente genéricos.
    """
    hoje = datetime.now().date()

    condicoes = [
        "a.ativo = TRUE", "a.posicao = %s",
        "(a.data_inicio IS NULL OR a.data_inicio <= %s)",
        "(a.data_fim IS NULL OR a.data_fim >= %s)",
    ]
    params = [posicao, hoje, hoje]

    if contexto == 'funcionalidade':
        condicoes.append("""(
            a.apenas_funcionalidades = TRUE
            OR (a.apenas_funcionalidades = FALSE AND a.ocasiao_id IS NULL
                AND a.tipo_impressao_id IS NULL AND a.cidade_slug IS NULL)
        )""")
    else:
        condicoes.append("a.apenas_funcionalidades = FALSE")
        condicoes.append("(a.ocasiao_id IS NULL OR a.ocasiao_id = %s)")
        params.append(ocasiao_id)
        condicoes.append("(a.tipo_impressao_id IS NULL OR a.tipo_impressao_id = %s)")
        params.append(tipo_impressao_id)
        condicoes.append("(a.cidade_slug IS NULL OR a.cidade_slug = %s)")
        params.append(cidade_slug)

    sql = f"""
        SELECT a.*, o.nome as ocasiao_nome
        FROM brindes_anuncios a
        LEFT JOIN brindes_ocasioes o ON o.id = a.ocasiao_id
        WHERE {' AND '.join(condicoes)}
        ORDER BY (
            (a.ocasiao_id IS NOT NULL)::int +
            (a.tipo_impressao_id IS NOT NULL)::int +
            (a.cidade_slug IS NOT NULL)::int +
            (a.apenas_funcionalidades IS TRUE)::int
        ) DESC, RANDOM()
        LIMIT 1
    """
    return query_one(sql, tuple(params))


# --- ROTA RAIZ ---
@app.route('/')
def index():
    # Tipos de impressão, Brindes e Empresas agora são carregados no cliente via
    # fetch em /api/tipos-impressao, /api/brindes e /api/empresas (padrão de
    # chip + grid). Sem limiar mínimo de itens: as seções sempre aparecem.
    return render_template(
        'home.html',
        current_year=datetime.now().year,
        anuncio_topo=get_anuncio('topo'),
        anuncio_meio=get_anuncio('meio'),
        templates=carregar_templates(),
    )


# --- ACESSAR PAINEL (telinha de "digite seu link/slug", redireciona pro login certo) ---
@app.route('/acessar-painel', methods=['GET', 'POST'])
def acessar_painel():
    erro = None
    if request.method == 'POST':
        if not check_limit(f"acessar_painel_{get_ip()}", 15, 3600):
            flash("Muitas tentativas. Tente novamente mais tarde.", "error")
            return redirect('/acessar-painel')

        bruto = (request.form.get('slug') or '').strip()

        # Aceita tanto o link completo colado (com ou sem https://, com ou sem barra
        # no final) quanto só o código/slug puro digitado direto.
        slug = bruto.lower()
        slug = re.sub(r'^https?://', '', slug)
        slug = slug.strip('/').split('/')[0].split('?')[0]
        # Se a primeira parte parece um domínio (tem ponto) e havia mais barra
        # depois, o slug real é o próximo pedaço do caminho.
        if '.' in slug and '/' in bruto.lower().strip():
            partes = re.sub(r'^https?://', '', bruto.lower()).strip('/').split('/')
            slug = partes[1].split('?')[0] if len(partes) > 1 else slug

        pagina = get_pagina_by_slug(slug) if slug else None
        if not pagina:
            erro = "Não encontramos essa página. Confira o link ou código e tente de novo."
        else:
            return redirect(f'/{slug}/login')

    return render_template('acessar_painel.html', erro=erro)


# --- GERAR QR CODE (criação de página) ---
@app.route('/gerar-qr', methods=['GET', 'POST'])
def gerar_qr():
    if not check_limit(f"gerar_{get_ip()}", 10, 3600):
        flash("Muitas tentativas. Tente novamente mais tarde.", "error")
        return redirect('/')

    if request.method == 'GET':
        # Vem do hero da home (form GET com ?destino_url=...) já com o link colado
        destino_url_prefill = request.args.get('destino_url', '').strip()
        return render_template(
            'gerar_qr.html',
            templates=carregar_templates(),
            current_year=datetime.now().year,
            anuncio_topo=get_anuncio('topo', contexto='funcionalidade'),
            destino_url_prefill=destino_url_prefill,
        )

    if request.method == 'POST':
        slug = request.form.get('slug', '').lower().strip()
        senha = request.form.get('senha', '')
        tipo_destino = request.form.get('tipo_destino', 'link')
        destino_url = request.form.get('destino_url', '').strip()
        email = request.form.get('email', '').strip().lower()
        titulo = request.form.get('titulo', '').strip()
        template_slug = request.form.get('template', 'classic')

        if not slug or not senha:
            flash('Preencha o link (slug) e a senha.', 'error')
            return render_template('gerar_qr.html', templates=carregar_templates(), current_year=datetime.now().year, anuncio_topo=get_anuncio('topo', contexto='funcionalidade'))

        if not slug_disponivel(slug):
            flash('Esse link já está em uso, escolha outro.', 'error')
            return render_template('gerar_qr.html', templates=carregar_templates(), current_year=datetime.now().year, anuncio_topo=get_anuncio('topo', contexto='funcionalidade'))

        if tipo_destino == 'pagina' and not email:
            flash('Email é obrigatório para criar uma página própria (usado para pagamento e recuperação).', 'error')
            return render_template('gerar_qr.html', templates=carregar_templates(), current_year=datetime.now().year, anuncio_topo=get_anuncio('topo', contexto='funcionalidade'))

        if email and not EMAIL_REGEX.match(email):
            flash('Digite um e-mail válido.', 'error')
            return render_template('gerar_qr.html', templates=carregar_templates(), current_year=datetime.now().year, anuncio_topo=get_anuncio('topo', contexto='funcionalidade'))

        if email and email_ja_usado_nesse_template(email, template_slug):
            flash('Esse e-mail já tem um QR code criado com esse mesmo template. Escolha um template diferente pra testar outro, ou entre no QR code que você já criou.', 'error')
            return render_template('gerar_qr.html', templates=carregar_templates(), current_year=datetime.now().year, anuncio_topo=get_anuncio('topo', contexto='funcionalidade'))

        if tipo_destino == 'link' and not destino_url:
            flash('Cole o link de destino.', 'error')
            return render_template('gerar_qr.html', templates=carregar_templates(), current_year=datetime.now().year, anuncio_topo=get_anuncio('topo', contexto='funcionalidade'))

        if tipo_destino == 'pagina':
            tpl_escolhido = get_template_por_slug(template_slug)
            if tpl_escolhido and tpl_escolhido.get('tier') == 'pago':
                # Template pago: salva a página como 'pendente' (ativo=FALSE)
                # em vez de criar ativada — o slug fica reservado
                # (PIX_RESERVA_HORAS) e some do banco na prática assim que
                # outro slug igual for tentado após expirar, sem cron.
                # Some do público porque get_pagina_by_slug só enxerga ativo=TRUE.
                execute_returning("""
                    INSERT INTO brindes_paginas
                        (slug, senha_hash, tipo_destino, destino_url, template, titulo, email, plano, ativo)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'pendente', FALSE)
                    RETURNING id
                """, (
                    slug,
                    generate_password_hash(senha),
                    tipo_destino,
                    None,
                    template_slug,
                    titulo,
                    email or None,
                ))

                preco = float(tpl_escolhido.get('preco') or 0)
                pix_payload = None
                pix_qr_base64 = None
                whatsapp_link = None

                if PIX_CHAVE and PIX_NOME_RECEBEDOR and PIX_CIDADE and preco > 0:
                    pix_payload = gerar_pix_payload(
                        PIX_CHAVE, PIX_NOME_RECEBEDOR, PIX_CIDADE, preco, slug
                    )
                    pix_qr_base64 = gerar_pix_qr_base64(pix_payload)

                if WHATSAPP_COMPROVANTE:
                    msg = (
                        f"Oi! Paguei o modelo \"{tpl_escolhido.get('nome')}\" "
                        f"(R$ {preco:.2f}). Meu link escolhido foi: {slug}. Segue o comprovante:"
                    )
                    whatsapp_link = f"https://wa.me/{WHATSAPP_COMPROVANTE}?text={quote(msg)}"

                return render_template(
                    'pagamento_pendente.html',
                    template=tpl_escolhido,
                    slug=slug,
                    pix_payload=pix_payload,
                    pix_qr_base64=pix_qr_base64,
                    whatsapp_link=whatsapp_link,
                    link_pagamento=LINK_PAGAMENTO_TEMPLATES,
                    current_year=datetime.now().year,
                )

        pagina_id = execute_returning("""
            INSERT INTO brindes_paginas
                (slug, senha_hash, tipo_destino, destino_url, template, titulo, email, plano, ativo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'gratis', TRUE)
            RETURNING id
        """, (
            slug,
            generate_password_hash(senha),
            tipo_destino,
            destino_url if tipo_destino == 'link' else None,
            template_slug,
            titulo,
            email or None,
        ))

        session['pagina_id'] = pagina_id
        session['pagina_slug'] = slug

        flash('QR code criado com sucesso!', 'success')
        return redirect(f'/{slug}/painel')


# --- REDIRECIONADOR DO QR FÍSICO ---
@app.route('/q/<slug>')
def redirecionador_qr(slug):
    pagina = get_pagina_by_slug(slug)
    if not pagina:
        return redirect('/')

    registrar_scan(pagina['id'])

    if pagina['tipo_destino'] == 'link' and pagina['destino_url']:
        return redirect(pagina['destino_url'])

    return render_pagina(pagina)


# --- ACESSO DIRETO POR LINK (também conta como scan) ---
@app.route('/<slug>')
def perfil_publico(slug):
    slug = slug.lower().strip()
    if slug in ['static', 'favicon.ico', 'gerar-qr', 'diretorio', 'ocasiao', 'brinde',
                'empresa', 'impressao', 'cidade', 'demo']:
        abort(404)

    pagina = get_pagina_by_slug(slug)
    if not pagina:
        abort(404)

    registrar_scan(pagina['id'])

    if pagina['tipo_destino'] == 'link' and pagina['destino_url']:
        return redirect(pagina['destino_url'])

    return render_pagina(pagina)


def render_pagina(pagina):
    templates_disponiveis = carregar_templates()
    tpl = get_template_por_slug(pagina.get('template'))
    if not tpl and templates_disponiveis:
        tpl = templates_disponiveis[0]
    if not tpl:
        abort(404)
    total_scans_row = query_one("SELECT COUNT(*) as total FROM brindes_scans WHERE pagina_id = %s", (pagina['id'],))
    total_scans = total_scans_row['total'] if total_scans_row else 0
    page, carimbos_atual = montar_page_context(pagina)
    return render_template(
        tpl['arquivo'],
        page=page,
        timeline_events=gerar_timeline_events(pagina),
        current_year=datetime.now().year,
        font_css="'Inter', sans-serif",
        font_size_val="1.1rem",
        total_scans=total_scans,
        carimbos_atual=carimbos_atual,
    )


# --- DEMO DE TEMPLATE (preview ao vivo, sem gravar nada no banco) ---
@app.route('/demo/<template_slug>')
def demo_template(template_slug):
    tpl = get_template_por_slug(template_slug)
    if not tpl:
        abort(404)
    pagina_fake = {**tpl.get('demo', {}), 'template': template_slug}
    total_scans = tpl.get('demo_total_scans', 7)
    return render_template(
        tpl['arquivo'], page=pagina_fake,
        timeline_events=pagina_fake.get('timeline', []),
        current_year=datetime.now().year,
        font_css="'Inter', sans-serif", font_size_val="1.1rem",
        anuncio_topo=get_anuncio('topo', contexto='funcionalidade'),
        total_scans=total_scans,
        carimbos_atual=tpl.get('demo_carimbos_atual', 0),
    )


# --- QR CODE EM PNG (pra imprimir) ---
@app.route('/<slug>/qr.png')
def qr_png(slug):
    pagina = get_pagina_by_slug(slug)
    if not pagina:
        abort(404)

    url_destino = f"{BASE_URL}/q/{slug}"
    img = qrcode.make(url_destino)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png')


# ═══════════════════════════════════════════════════════════════════════
# FIDELIZE — SaaS de fidelidade B2B (fidelize.qrcodebrindes.com.br)
# Mesmo app, mesmo banco. Isolado por subdomain='fidelize' em cada rota.
# Reaproveita (NÃO duplica): render_pagina, carregar_templates,
# get_template_por_slug, geração de QR (qrcode.make), query_one/query_all/
# execute/execute_returning, generate_password_hash/check_password_hash,
# check_limit/get_ip, gerar_senha_aleatoria e slugify_cidade — tudo já
# definido acima. A rota pública /q/<slug> e /<slug> (sem subdomínio)
# continuam sendo o único ponto de entrada dos clientes finais, tenham eles
# conta_id ou não — nada muda lá.
#
# Chave de sessão própria (fidelize_conta_id), separada de pagina_id /
# admin_id, pra não conflitar com os outros logins do projeto.
# ═══════════════════════════════════════════════════════════════════════

def get_conta_fidelize_by_email(email):
    return query_one(
        "SELECT * FROM brindes_fidelidade_contas WHERE email = %s",
        (email,)
    )


def fidelize_login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('fidelize_conta_id'):
            return redirect('/login')
        return f(*args, **kwargs)
    return decorated


def _fidelize_gerar_slug_unico(nome_negocio):
    """Slug público pra página de fidelidade: nome do negócio + sufixo
    aleatório curto, verificando colisão contra brindes_paginas."""
    base = slugify_cidade(nome_negocio) or 'fidelidade'
    for _ in range(20):
        candidato = f"{base}-{secrets.token_hex(3)}"
        if not get_pagina_by_slug(candidato):
            return candidato
    return f"fidelidade-{secrets.token_hex(6)}"  # fallback, praticamente nunca deve cair aqui


@app.route('/', subdomain='fidelize')
def fidelize_home():
    # Só os templates da família "gamificacao*" (mesmo prefixo usado no
    # painel) — a home do Fidelize é vitrine pública, sem conta logada, então
    # não passa por conta_pode_usar_template aqui: mostra todos os
    # gamificacao*, inclusive os marcados como "exclusivo" (pago), só pra
    # ela ver que existem — a restrição de uso real continua acontecendo
    # dentro do painel, na hora de trocar de template.
    templates_gamificacao = [
        t for t in carregar_templates() if t['slug'].startswith('gamificacao')
    ]
    return render_template(
        'fidelize/home.html',
        current_year=datetime.now().year,
        qr_codes_trial=FIDELIZE_QR_CODES_TRIAL,
        limite_qr_codes=FIDELIZE_LIMITE_QR_CODES_POR_CONTA,
        preco_mensalidade=FIDELIZE_PRECO_MENSALIDADE,
        templates=templates_gamificacao,
    )


@app.route('/cadastro', methods=['GET', 'POST'], subdomain='fidelize')
def fidelize_cadastro():
    erro = None
    if request.method == 'POST':
        if not check_limit(f"fidelize_cadastro_{get_ip()}", 10, 3600):
            flash('Muitas tentativas. Tente novamente mais tarde.', 'error')
            return redirect('/cadastro')

        nome_negocio = (request.form.get('nome_negocio') or '').strip()
        email = (request.form.get('email') or '').strip().lower()
        senha = request.form.get('senha') or ''

        if not nome_negocio or not email or not senha:
            erro = 'Preencha todos os campos.'
        elif get_conta_fidelize_by_email(email):
            erro = 'Já existe uma conta com esse e-mail.'
        else:
            conta_id = execute_returning("""
                INSERT INTO brindes_fidelidade_contas
                    (nome_negocio, email, senha_hash, plano_tipo, qr_codes_disponiveis, mensalidade_ativa, ativo)
                VALUES (%s, %s, %s, 'pacote', %s, FALSE, TRUE)
                RETURNING id
            """, (nome_negocio, email, generate_password_hash(senha), FIDELIZE_QR_CODES_TRIAL))

            session['fidelize_conta_id'] = conta_id
            session['fidelize_conta_nome'] = nome_negocio
            flash(f'Conta criada com sucesso! Você já ganhou {FIDELIZE_QR_CODES_TRIAL} QR codes grátis pra testar.', 'success')
            return redirect('/painel')

    return render_template(
        'fidelize/cadastro.html',
        erro=erro,
        current_year=datetime.now().year,
        qr_codes_trial=FIDELIZE_QR_CODES_TRIAL,
    )


@app.route('/login', methods=['GET', 'POST'], subdomain='fidelize')
def fidelize_login():
    erro = None
    if request.method == 'POST':
        if not check_limit(f"fidelize_login_{get_ip()}", 10, 60):
            flash('Muitas tentativas. Aguarde.', 'error')
            return render_template('fidelize/login.html', current_year=datetime.now().year)

        email = (request.form.get('email') or '').strip().lower()
        senha = request.form.get('senha') or ''
        conta = get_conta_fidelize_by_email(email)

        if conta and conta['ativo'] and check_password_hash(conta['senha_hash'], senha):
            session['fidelize_conta_id'] = conta['id']
            session['fidelize_conta_nome'] = conta['nome_negocio']
            # Se ela veio de um /<slug>/painel ou /<slug>/carimbar sem sessão
            # válida (ex: primeira visita ao apex depois de logar só no
            # subdomínio), volta pra lá em vez de sempre cair no /painel.
            destino = session.pop('fidelize_redirect_apos_login', None)
            return redirect(destino or '/painel')
        erro = 'E-mail ou senha incorretos.'

    return render_template('fidelize/login.html', erro=erro, current_year=datetime.now().year)


@app.route('/painel', subdomain='fidelize')
@fidelize_login_required
def fidelize_painel():
    conta_id = session['fidelize_conta_id']
    conta = query_one("SELECT * FROM brindes_fidelidade_contas WHERE id = %s", (conta_id,))
    if not conta:
        session.pop('fidelize_conta_id', None)
        return redirect('/login')

    paginas = query_all("""
        SELECT p.id, p.slug, p.template, p.titulo, p.mensagem, p.foto_url,
               p.apelido_slug, p.campos_extra,
               (SELECT COUNT(*) FROM brindes_scans s WHERE s.pagina_id = p.id) AS total_scans
        FROM brindes_paginas p
        WHERE p.conta_id = %s
        ORDER BY p.id DESC
    """, (conta_id,))

    # Mesmo teto usado em fidelize_criar_qr — mandado pro template só pra
    # exibir "X de 100 QR codes usados" no painel, não é a validação em si
    # (a validação real acontece de novo na hora de criar, servidor-side).
    total_qr_codes_conta = len(paginas)

    # Saldo grátis/pacote esgotado e sem mensalidade ativa = ela precisa
    # assinar pra continuar criando. Calculado aqui pra mostrar o banner de
    # cobrança no painel antes mesmo dela tentar criar e tomar o erro.
    saldo_esgotado = (
        not conta['mensalidade_ativa']
        and conta['qr_codes_disponiveis'] <= 0
        and total_qr_codes_conta < FIDELIZE_LIMITE_QR_CODES_POR_CONTA
    )

    templates_disponiveis = carregar_templates()
    campos_por_template = {t['slug']: t.get('campos', []) for t in templates_disponiveis}

    # Só os templates da família "gamificacao*" (prefixo no slug) — o modal
    # de edição do Fidelize só pode oferecer troca entre esses, nunca o
    # catálogo geral (presente/ocasião) do QRCodeBrindes. Além disso, filtra
    # os marcados como "exclusivo" no .json: só entram na lista se essa
    # conta específica tiver o slug liberado em templates_extras (ver
    # conta_pode_usar_template) — assim um template vendido separadamente
    # não aparece pra quem não pagou por ele.
    templates_gamificacao = [
        t for t in templates_disponiveis
        if t['slug'].startswith('gamificacao') and conta_pode_usar_template(conta, t)
    ]
    # Mandado pro template como JSON pra alimentar o JS do modal (troca de
    # template re-renderiza os campos extras no navegador, sem reload).
    templates_gamificacao_json = json.dumps([
        {'slug': t['slug'], 'nome': t.get('nome', t['slug']), 'campos': t.get('campos', [])}
        for t in templates_gamificacao
    ])

    # União (sem duplicar por nome) de todos os campos extras de qualquer
    # template da família "gamificacao*" — alimenta o formulário de edição
    # em massa. Cada página, na hora de salvar, só recebe os campos que o
    # SEU PRÓPRIO template realmente tem (ver fidelize_aplicar_campos_todos);
    # os demais são simplesmente ignorados pra aquela página, sem erro.
    campos_bulk_vistos = set()
    campos_bulk_uniao = []
    for t in templates_gamificacao:
        for c in t.get('campos', []):
            nome_c = c.get('nome')
            if not nome_c or nome_c in ('titulo', 'mensagem', 'timeline') or nome_c in campos_bulk_vistos:
                continue
            campos_bulk_vistos.add(nome_c)
            campos_bulk_uniao.append(c)

    # Prefixo fixo do link (nome do negócio slugificado + '-'), pra manter a
    # marca no link mesmo quando ela edita a parte de trás. Mesma função
    # (slugify_cidade) usada em _fidelize_gerar_slug_unico, pra consistência.
    prefixo_slug = (slugify_cidade(conta['nome_negocio']) or 'fidelidade') + '-'
    for p in paginas:
        # sufixo_atual é só o "apelido" que ela digitou (ex: teste1) — o
        # sufixo aleatório escondido que garante o link único (ex: -a1b2)
        # não aparece aqui, fica só dentro de p['slug'] (o link real).
        if p.get('apelido_slug'):
            p['sufixo_atual'] = p['apelido_slug']
        else:
            p['sufixo_atual'] = p['slug'][len(prefixo_slug):] if p['slug'].startswith(prefixo_slug) else p['slug']

        # Carimbos atuais do cartão, pra exibir e permitir carimbar sem sair
        # do painel do Fidelize (evita depender de sessão entre subdomínio e
        # apex, que ainda tá sendo ajustada na infra).
        campos_extra = p.get('campos_extra') or {}
        if isinstance(campos_extra, str):
            try:
                campos_extra = json.loads(campos_extra)
            except (ValueError, TypeError):
                campos_extra = {}
        try:
            p['carimbos_atual'] = int(campos_extra.get('carimbos_atual', 0) or 0)
        except (ValueError, TypeError):
            p['carimbos_atual'] = 0

        # Dict já parseado, pra o modal de edição preencher os campos extras
        # do template (ex: meta, premio) sem precisar reprocessar no HTML.
        p['campos_extra_dict'] = campos_extra
        p['campos_template'] = campos_por_template.get(p['template'], [])

    return render_template(
        'fidelize/painel.html',
        conta=conta,
        paginas=paginas,
        prefixo_slug=prefixo_slug,
        base_url=BASE_URL,
        current_year=datetime.now().year,
        templates_gamificacao=templates_gamificacao,
        templates_gamificacao_json=templates_gamificacao_json,
        campos_bulk_uniao=campos_bulk_uniao,
        limite_qr_codes=FIDELIZE_LIMITE_QR_CODES_POR_CONTA,
        total_qr_codes_conta=total_qr_codes_conta,
        saldo_esgotado=saldo_esgotado,
        preco_mensalidade=FIDELIZE_PRECO_MENSALIDADE,
        qr_codes_trial=FIDELIZE_QR_CODES_TRIAL,
    )


# --- ASSINAR MENSALIDADE (Pix dinâmico, mesmo padrão do template pago) ---
@app.route('/painel/assinar', subdomain='fidelize')
@fidelize_login_required
def fidelize_assinar():
    conta_id = session['fidelize_conta_id']
    conta = query_one("SELECT * FROM brindes_fidelidade_contas WHERE id = %s", (conta_id,))
    if not conta:
        session.pop('fidelize_conta_id', None)
        return redirect('/login')

    preco = FIDELIZE_PRECO_MENSALIDADE
    pix_payload = None
    pix_qr_base64 = None
    whatsapp_link = None

    # txid leva o id da conta (não o slug — aqui não existe um slug único
    # de cobrança) pra você identificar de quem é o Pix na hora de conferir
    # e liberar em /admin#fidelize.
    if PIX_CHAVE and PIX_NOME_RECEBEDOR and PIX_CIDADE and preco > 0:
        pix_payload = gerar_pix_payload(
            PIX_CHAVE, PIX_NOME_RECEBEDOR, PIX_CIDADE, preco, f"FIDELIZE{conta_id}"
        )
        pix_qr_base64 = gerar_pix_qr_base64(pix_payload)

    if WHATSAPP_FIDELIZE:
        msg = (
            f"Oi! Paguei a mensalidade do Fidelize (R$ {preco:.2f}). "
            f"Minha conta: {conta['nome_negocio']} ({conta['email']}). Segue o comprovante:"
        )
        whatsapp_link = f"https://wa.me/{WHATSAPP_FIDELIZE}?text={quote(msg)}"

    return render_template(
        'fidelize/pagamento.html',
        conta=conta,
        preco=preco,
        limite_qr_codes=FIDELIZE_LIMITE_QR_CODES_POR_CONTA,
        pix_payload=pix_payload,
        pix_qr_base64=pix_qr_base64,
        whatsapp_link=whatsapp_link,
        current_year=datetime.now().year,
    )


@app.route('/painel/criar-qr', methods=['POST'], subdomain='fidelize')
@fidelize_login_required
def fidelize_criar_qr():
    conta_id = session['fidelize_conta_id']
    conta = query_one("SELECT * FROM brindes_fidelidade_contas WHERE id = %s", (conta_id,))
    if not conta:
        session.pop('fidelize_conta_id', None)
        return redirect('/login')

    if not (conta['mensalidade_ativa'] or conta['qr_codes_disponiveis'] > 0):
        flash(f'Seus QR codes grátis acabaram. Assine por R$ {FIDELIZE_PRECO_MENSALIDADE:.2f}/mês pra continuar criando.', 'error')
        return redirect('/painel')

    # Teto de QR codes por conta, mesmo pra quem já paga mensalidade (sem
    # isso, mensalidade_ativa=True liberaria criação ilimitada). Conta todas
    # as páginas da conta, sem filtrar por 'ativo' — desativar uma página
    # não libera vaga nova, é teto de "já criados", não de "em uso agora".
    total_qr_codes_conta = query_one(
        "SELECT COUNT(*) as c FROM brindes_paginas WHERE conta_id = %s",
        (conta_id,)
    )['c']
    if total_qr_codes_conta >= FIDELIZE_LIMITE_QR_CODES_POR_CONTA:
        flash(f'Você atingiu o limite de {FIDELIZE_LIMITE_QR_CODES_POR_CONTA} QR codes da conta. Fale com a gente se precisar de mais.', 'error')
        return redirect('/painel')

    slug = _fidelize_gerar_slug_unico(conta['nome_negocio'])
    # A coluna senha_hash é usada pelo login individual de /<slug>/login (fluxo
    # já existente do projeto); no fluxo fidelize o dono não usa essa senha —
    # ele gerencia tudo pelo /painel — mas geramos uma pra manter a coluna
    # preenchida e a Pagina 100% compatível com o resto da infra existente.
    senha_interna = gerar_senha_aleatoria()

    execute("""
        INSERT INTO brindes_paginas
            (slug, senha_hash, tipo_destino, template, titulo, email, plano, ativo, conta_id)
        VALUES (%s, %s, 'pagina', 'gamificacao', %s, %s, 'gratis', TRUE, %s)
    """, (
        slug,
        generate_password_hash(senha_interna),
        conta['nome_negocio'],
        conta['email'],
        conta_id,
    ))

    if conta['plano_tipo'] == 'pacote':
        execute(
            "UPDATE brindes_fidelidade_contas SET qr_codes_disponiveis = qr_codes_disponiveis - 1 WHERE id = %s",
            (conta_id,)
        )

    flash('QR code criado com sucesso!', 'success')
    return redirect('/painel')


# --- TROCAR O TEMPLATE DE TODOS OS QR CODES DE UMA VEZ ---
# Mesma validação de segurança do modal de edição individual (só aceita
# slug que comece com "gamificacao" e que exista de fato em disco) — só que
# aplicada a todas as páginas da conta de uma só vez, sem precisar abrir o
# modal QR code por QR code. Não mexe em campos_extra de nenhuma página: os
# valores continuam salvos, só passam a ser lidos pela lente do template
# novo (mesmo comportamento não-destrutivo da troca individual).
@app.route('/painel/aplicar-template-todos', methods=['POST'], subdomain='fidelize')
@fidelize_login_required
def fidelize_aplicar_template_todos():
    conta_id = session['fidelize_conta_id']
    conta = query_one("SELECT templates_extras FROM brindes_fidelidade_contas WHERE id = %s", (conta_id,))
    template_form = (request.form.get('template') or '').strip()
    template_meta = get_template_por_slug(template_form) if template_form else None

    if not (template_form.startswith('gamificacao') and template_meta
            and conta_pode_usar_template(conta or {}, template_meta)):
        flash('Modelo inválido.', 'error')
        return redirect('/painel')

    execute("""
        UPDATE brindes_paginas
        SET template = %s
        WHERE conta_id = %s AND template LIKE 'gamificacao%%'
    """, (template_form, conta_id))

    flash('Modelo aplicado a todos os seus QR codes!', 'success')
    return redirect('/painel')


# --- EDITAR TÍTULO/MENSAGEM/CAMPOS EXTRAS DE TODOS OS QR CODES DE UMA VEZ ---
# Sobrescreve título e mensagem com o mesmo valor em TODOS os cartões da
# conta (decisão consciente: é uma reescrita em massa mesmo, não um "aplicar
# só onde tiver vazio"). Os campos extras (ex: meta, prêmio) só são
# sobrescritos página por página SE o template daquela página específica
# realmente tiver aquele campo — como páginas podem estar em templates
# diferentes da família "gamificacao*", um campo que não existe no template
# de uma página é simplesmente ignorado pra ela, sem erro.
@app.route('/painel/aplicar-campos-todos', methods=['POST'], subdomain='fidelize')
@fidelize_login_required
def fidelize_aplicar_campos_todos():
    conta_id = session['fidelize_conta_id']
    paginas = query_all(
        "SELECT id, template, campos_extra, foto_url FROM brindes_paginas WHERE conta_id = %s",
        (conta_id,)
    )
    if not paginas:
        flash('Você ainda não tem nenhum QR code criado.', 'error')
        return redirect('/painel')

    titulo = request.form.get('titulo', '').strip()
    mensagem = request.form.get('mensagem', '')

    # Foto compartilhada é opcional — só sobe UMA vez (se ela escolher um
    # arquivo) e a mesma URL é reaproveitada em toda página cujo template
    # tenha campo de imagem.
    foto_url_compartilhada = None
    f = request.files.get('foto')
    if f and f.filename:
        foto_url_compartilhada = upload_imgur(f)
        if not foto_url_compartilhada:
            flash('Título/mensagem/campos foram salvos, mas houve erro ao enviar a foto compartilhada.', 'error')

    campos_fixos = {'titulo', 'mensagem', 'foto', 'imagem', 'timeline'}
    atualizados = 0

    for p in paginas:
        tpl = get_template_por_slug(p.get('template'))
        campos_tpl = (tpl or {}).get('campos', [])

        campos_extra_existentes = p.get('campos_extra') or {}
        if isinstance(campos_extra_existentes, str):
            try:
                campos_extra_existentes = json.loads(campos_extra_existentes)
            except (ValueError, TypeError):
                campos_extra_existentes = {}
        campos_extra = dict(campos_extra_existentes)

        for campo in campos_tpl:
            nome_campo = campo.get('nome')
            if not nome_campo or nome_campo in campos_fixos or nome_campo not in request.form:
                continue
            valor_form = request.form.get(nome_campo, '').strip()
            if campo.get('tipo') == 'numero':
                try:
                    campos_extra[nome_campo] = int(valor_form) if valor_form else None
                except ValueError:
                    campos_extra[nome_campo] = None
            else:
                campos_extra[nome_campo] = valor_form

        foto_url = p.get('foto_url')
        if foto_url_compartilhada and 'imagem' in {c.get('tipo') for c in campos_tpl}:
            foto_url = foto_url_compartilhada

        execute("""
            UPDATE brindes_paginas
            SET titulo = %s, mensagem = %s, foto_url = %s, campos_extra = %s
            WHERE id = %s
        """, (
            titulo,
            mensagem,
            foto_url,
            psycopg2.extras.Json(campos_extra),
            p['id'],
        ))
        atualizados += 1

    flash(f'{atualizados} cartão(ões) atualizado(s) com sucesso!', 'success')
    return redirect('/painel')


# --- EDITAR O LINK (slug) DE UM QR CODE — prefixo fixo (nome do negócio),
# só a parte de trás é livre pra ela escolher. O que ela digita ("apelido")
# fica salvo separado do slug real: o slug real leva um sufixo aleatório
# escondido (nunca aparece no formulário) pra ninguém adivinhar o link de
# outro cliente só testando nomes comuns. ---
@app.route('/painel/pagina/<int:pagina_id>/slug', methods=['POST'], subdomain='fidelize')
@fidelize_login_required
def fidelize_editar_slug(pagina_id):
    conta_id = session['fidelize_conta_id']
    conta = query_one("SELECT * FROM brindes_fidelidade_contas WHERE id = %s", (conta_id,))
    if not conta:
        session.pop('fidelize_conta_id', None)
        return redirect('/login')

    # WHERE conta_id = %s garante que ela só edita página da própria conta,
    # mesmo que alguém tente forjar o pagina_id na URL.
    pagina = query_one(
        "SELECT id, slug, apelido_slug FROM brindes_paginas WHERE id = %s AND conta_id = %s",
        (pagina_id, conta_id)
    )
    if not pagina:
        abort(404)

    prefixo_slug = (slugify_cidade(conta['nome_negocio']) or 'fidelidade') + '-'
    apelido = slugify_cidade(request.form.get('sufixo', ''))

    if not apelido:
        flash('Digite um complemento válido pro link (só letras, números e hífen).', 'error')
        return redirect('/painel')

    # Se o apelido não mudou, não mexe no link — evita gerar um sufixo novo
    # (e quebrar um link que ela já distribuiu) só porque clicou em "Salvar
    # link" de novo sem editar o texto.
    if apelido == pagina.get('apelido_slug'):
        return redirect('/painel')

    novo_slug = None
    for _ in range(20):
        candidato = f"{prefixo_slug}{apelido}-{secrets.token_hex(2)}"
        colisao = query_one("SELECT id FROM brindes_paginas WHERE slug = %s", (candidato,))
        if not colisao:
            novo_slug = candidato
            break

    if not novo_slug:
        flash('Não foi possível gerar um link único agora, tenta de novo.', 'error')
        return redirect('/painel')

    execute(
        "UPDATE brindes_paginas SET slug = %s, apelido_slug = %s WHERE id = %s",
        (novo_slug, apelido, pagina_id)
    )
    flash('Link atualizado com sucesso!', 'success')

    return redirect('/painel')


@app.route('/logout', subdomain='fidelize')
def fidelize_logout():
    # session.pop específico (não session.clear()) pra não derrubar, por
    # exemplo, uma sessão de /<slug>/login aberta na mesma aba/domínio.
    session.pop('fidelize_conta_id', None)
    session.pop('fidelize_conta_nome', None)
    return redirect('/')


# --- CARIMBAR DIRETO DO PAINEL DO FIDELIZE ---
# Mesma lógica de carimbar_pagina (/<slug>/carimbar), mas rodando 100% dentro
# do subdomínio fidelize.qrcodebrindes.com.br — não depende da sessão viajar
# pro apex (qrcodebrindes.com.br), que é o que tava causando o loop de login.
# A checagem "WHERE conta_id = %s" garante que ela só carimba página da
# própria conta, sem precisar de pode_gerenciar_pagina/sessão cruzada.
# --- EDITAR OS CAMPOS DO CARTÃO (modal) DIRETO DO PAINEL DO FIDELIZE ---
# Mesma ideia da rota de carimbar: fica 100% dentro do subdomínio fidelize,
# sem depender da sessão viajar pro apex. Só edita título/mensagem/foto e os
# campos extras do template ATUAL (ex: meta, premio do Cartão Fidelidade) —
# não permite trocar de template por aqui, isso continua no painel completo
# (/<slug>/painel) no apex.
@app.route('/painel/pagina/<int:pagina_id>/editar', methods=['POST'], subdomain='fidelize')
@fidelize_login_required
def fidelize_editar_pagina(pagina_id):
    conta_id = session['fidelize_conta_id']
    pagina = query_one(
        "SELECT * FROM brindes_paginas WHERE id = %s AND conta_id = %s",
        (pagina_id, conta_id)
    )
    if not pagina:
        abort(404)

    template_atual = pagina.get('template', 'classic')
    template_form = (request.form.get('template') or '').strip()

    # Segurança: só aceita trocar de template se o valor enviado pelo form
    # (a) começar com "gamificacao", (b) existir de fato em disco e (c) essa
    # conta ter direito a ele (templates marcados "exclusivo" no .json só
    # liberam se o slug estiver em conta['templates_extras']) — nunca
    # confia cegamente no que vem do form, pra ela não conseguir (por engano
    # ou manipulação de request) trocar pro catálogo geral (presente/ocasião)
    # do QRCodeBrindes, nem pra um template exclusivo de outra conta. Se o
    # valor não passar na validação, mantém o template que a página já tinha.
    conta_da_pagina = query_one("SELECT templates_extras FROM brindes_fidelidade_contas WHERE id = %s", (conta_id,))
    template_meta_form = get_template_por_slug(template_form) if template_form else None
    if (template_form and template_form.startswith('gamificacao')
            and template_meta_form and conta_pode_usar_template(conta_da_pagina or {}, template_meta_form)):
        novo_template = template_form
    else:
        novo_template = template_atual

    # Os campos extras processados abaixo são os do template NOVO (não do
    # antigo) — assim, ao trocar de template, o form já valida/salva os
    # campos certos pro template escolhido.
    tpl_selecionado = get_template_por_slug(novo_template)
    campos_tpl = (tpl_selecionado or {}).get('campos', [])
    tipos_presentes = {c.get('tipo') for c in campos_tpl}

    # Só mexe em foto_url se o template atual realmente tiver campo de
    # imagem — mesma regra do painel completo, pra não apagar a foto antiga
    # quando o campo nem aparece no formulário.
    foto_url = pagina.get('foto_url')
    if 'imagem' in tipos_presentes:
        f = request.files.get('foto')
        if f and f.filename:
            nova_url = upload_imgur(f)
            if nova_url:
                foto_url = nova_url
            else:
                flash('Dados salvos, mas houve erro ao enviar a foto.', 'error')

    campos_extra_existentes = pagina.get('campos_extra') or {}
    if isinstance(campos_extra_existentes, str):
        try:
            campos_extra_existentes = json.loads(campos_extra_existentes)
        except (ValueError, TypeError):
            campos_extra_existentes = {}
    campos_extra = dict(campos_extra_existentes)
    campos_fixos = {'titulo', 'mensagem', 'foto', 'imagem', 'timeline'}
    for campo in campos_tpl:
        nome_campo = campo.get('nome')
        if not nome_campo or nome_campo in campos_fixos:
            continue
        valor_form = request.form.get(nome_campo, '').strip()
        if campo.get('tipo') == 'numero':
            try:
                campos_extra[nome_campo] = int(valor_form) if valor_form else None
            except ValueError:
                campos_extra[nome_campo] = None
        else:
            campos_extra[nome_campo] = valor_form
    campos_extra_json = psycopg2.extras.Json(campos_extra)

    execute("""
        UPDATE brindes_paginas
        SET titulo = %s, mensagem = %s, foto_url = %s, campos_extra = %s, template = %s
        WHERE id = %s
    """, (
        request.form.get('titulo', ''),
        request.form.get('mensagem', ''),
        foto_url,
        campos_extra_json,
        novo_template,
        pagina_id,
    ))

    flash('Cartão atualizado com sucesso!', 'success')
    return redirect('/painel')


@app.route('/painel/pagina/<int:pagina_id>/carimbar', methods=['POST'], subdomain='fidelize')
@fidelize_login_required
def fidelize_carimbar(pagina_id):
    conta_id = session['fidelize_conta_id']
    pagina = query_one(
        "SELECT id, campos_extra FROM brindes_paginas WHERE id = %s AND conta_id = %s",
        (pagina_id, conta_id)
    )
    if not pagina:
        abort(404)

    campos_extra = pagina.get('campos_extra') or {}
    if isinstance(campos_extra, str):
        try:
            campos_extra = json.loads(campos_extra)
        except (ValueError, TypeError):
            campos_extra = {}
    try:
        atual = int(campos_extra.get('carimbos_atual', 0) or 0)
    except (ValueError, TypeError):
        atual = 0

    acao = request.form.get('acao', 'somar')
    if acao == 'zerar':
        atual = 0
    else:
        atual += 1
    campos_extra['carimbos_atual'] = atual

    execute(
        "UPDATE brindes_paginas SET campos_extra = %s WHERE id = %s",
        (psycopg2.extras.Json(campos_extra), pagina_id),
    )
    flash('Carimbo zerado.' if acao == 'zerar' else 'Carimbo adicionado!', 'success')
    return redirect('/painel')


# --- LOGIN DA PÁGINA (slug + senha) ---
def pode_gerenciar_pagina(pagina):
    """Quem pode editar/carimbar uma Pagina: (a) quem logou individualmente
    nela (session['pagina_id']), OU (b) o dono da conta fidelize à qual essa
    Pagina pertence (session['fidelize_conta_id'] == pagina['conta_id']) —
    assim o dono do negócio gerencia o cartão pelo /painel do Fidelize sem
    precisar da senha individual aleatória gerada em fidelize_criar_qr."""
    if session.get('pagina_id') == pagina['id']:
        return True
    conta_id = pagina.get('conta_id')
    if conta_id and session.get('fidelize_conta_id') == conta_id:
        return True
    return False


@app.route('/<slug>/login', methods=['GET', 'POST'])
def login_pagina(slug):
    if not check_limit(f"login_{get_ip()}", 10, 60):
        flash("Muitas tentativas. Aguarde.", "error")
        return render_template('login.html', slug=slug)

    pagina = get_pagina_by_slug(slug)
    if not pagina:
        abort(404)

    if request.method == 'POST':
        senha = request.form.get('senha', '')
        if check_password_hash(pagina['senha_hash'], senha):
            session['pagina_id'] = pagina['id']
            session['pagina_slug'] = pagina['slug']
            return redirect(f'/{slug}/painel')
        flash('Senha incorreta.', 'error')

    return render_template('login.html', slug=slug)


# --- ESQUECI MINHA SENHA (self-service, sem precisar do admin) ---
@app.route('/<slug>/esqueci-senha', methods=['GET', 'POST'])
def esqueci_senha(slug):
    pagina = get_pagina_by_slug(slug)
    if not pagina:
        abort(404)

    if request.method == 'POST':
        if not check_limit(f"esqueci_{get_ip()}", 5, 600):
            flash("Muitas tentativas. Aguarde um pouco antes de tentar de novo.", "error")
            return render_template('esqueci_senha.html', slug=slug)

        email_digitado = request.form.get('email', '').strip().lower()

        # Mensagem sempre genérica na tela, mesmo se o e-mail não bater —
        # evita que alguém descubra por tentativa se um e-mail está cadastrado
        # numa página de outra pessoa.
        email_cadastrado = (pagina.get('email') or '').strip().lower()

        if email_cadastrado and email_digitado == email_cadastrado:
            senha_nova = gerar_senha_aleatoria()
            execute(
                "UPDATE brindes_paginas SET senha_hash = %s WHERE id = %s",
                (generate_password_hash(senha_nova), pagina['id'])
            )
            link_login = f"{BASE_URL}/{slug}/login"
            corpo = (
                f"Olá!\n\n"
                f"Recebemos um pedido de redefinição de senha para o seu QR code "
                f"({BASE_URL}/{slug}).\n\n"
                f"Nova senha: {senha_nova}\n"
                f"Link de login: {link_login}\n\n"
                f"Se você não pediu isso, ignore este e-mail — sua senha antiga "
                f"deixou de funcionar, então é recomendável entrar e definir uma nova "
                f"o quanto antes.\n\n"
                f"— QRCodeBrindes"
            )
            enviar_email(pagina['email'], "Nova senha do seu QRCodeBrindes", corpo)
            # não checamos o resultado do envio aqui de propósito — a mensagem
            # pra o usuário é a mesma em qualquer caso, por segurança

        flash(
            'Se o e-mail informado estiver correto e cadastrado, '
            'enviamos uma nova senha para ele.',
            'success'
        )
        return redirect(f'/{slug}/login')

    return render_template('esqueci_senha.html', slug=slug)


@app.route('/<slug>/logout')
def logout_pagina(slug):
    # session.pop específico (não session.clear()) — se quem saiu foi o dono
    # de uma conta fidelize gerenciando essa página pelo /painel dele, não
    # queremos derrubar a sessão fidelize_conta_id junto.
    session.pop('pagina_id', None)
    session.pop('pagina_slug', None)
    if session.get('fidelize_conta_id'):
        return redirect(f'{FIDELIZE_BASE_URL}/painel')
    return redirect(f'/{slug}')


# --- PAINEL DA PÁGINA (edição) ---
@app.route('/<slug>/painel', methods=['GET', 'POST'])
def painel_pagina(slug):
    pagina = get_pagina_by_slug(slug)
    if not pagina:
        abort(404)

    if not pode_gerenciar_pagina(pagina):
        if pagina.get('conta_id'):
            # Guarda pra onde ela queria ir — sem isso, fidelize_login()
            # manda sempre pro /painel (lista), nunca pro painel dessa
            # página específica, e ela nunca chega no carimbo.
            session['fidelize_redirect_apos_login'] = f'{BASE_URL}/{slug}/painel'
            flash('Faça login na sua conta Fidelize pra gerenciar esse QR code.', 'error')
            return redirect(f'{FIDELIZE_BASE_URL}/login')
        return redirect(f'/{slug}/login')

    templates_disponiveis = carregar_templates()

    if request.method == 'POST':
        template_slug = request.form.get('template', pagina.get('template', 'classic'))
        tpl_selecionado = get_template_por_slug(template_slug)
        campos_tpl = (tpl_selecionado or {}).get('campos', [])
        tipos_presentes = {c.get('tipo') for c in campos_tpl}

        # Só mexe em foto_url se o template atual realmente tiver campo de
        # imagem — evita apagar o valor antigo quando o campo nem aparece
        # no formulário (ex: template "Simples").
        foto_url = pagina.get('foto_url')
        if 'imagem' in tipos_presentes:
            f = request.files.get('foto')
            if f and f.filename:
                nova_url = upload_imgur(f)
                if nova_url:
                    foto_url = nova_url
                else:
                    flash('Dados salvos, mas houve erro ao enviar a foto.', 'error')

        # Timeline: só grava se o template atual tiver o campo, validando o
        # JSON serializado pelo JS antes de mandar pro banco.
        timeline_json = pagina.get('timeline_json')
        if 'timeline' in tipos_presentes:
            timeline_raw = request.form.get('timeline_json', '')
            try:
                linhas = json.loads(timeline_raw) if timeline_raw else []
                if not isinstance(linhas, list):
                    linhas = []
            except (ValueError, TypeError):
                linhas = []
            timeline_json = psycopg2.extras.Json(linhas)

        # Campos extras dinâmicos definidos no .json do template (ex: 'meta'
        # e 'premio' do Cartão Fidelidade). Os campos fixos (titulo, mensagem,
        # foto, timeline) já são tratados à parte acima/abaixo — aqui só
        # cuidamos do que sobra. Mantém o que já existia (incluindo
        # carimbos_atual, que não é editável nesse formulário) e sobrescreve
        # só com os campos que o template atual realmente tem.
        campos_extra_existentes = pagina.get('campos_extra') or {}
        if isinstance(campos_extra_existentes, str):
            try:
                campos_extra_existentes = json.loads(campos_extra_existentes)
            except (ValueError, TypeError):
                campos_extra_existentes = {}
        campos_extra = dict(campos_extra_existentes)
        campos_fixos = {'titulo', 'mensagem', 'foto', 'imagem', 'timeline'}
        for campo in campos_tpl:
            nome_campo = campo.get('nome')
            if not nome_campo or nome_campo in campos_fixos:
                continue
            valor_form = request.form.get(nome_campo, '').strip()
            if campo.get('tipo') == 'numero':
                try:
                    campos_extra[nome_campo] = int(valor_form) if valor_form else None
                except ValueError:
                    campos_extra[nome_campo] = None
            else:
                campos_extra[nome_campo] = valor_form
        campos_extra_json = psycopg2.extras.Json(campos_extra)

        execute("""
            UPDATE brindes_paginas
            SET titulo = %s,
                mensagem = %s,
                foto_url = %s,
                template = %s,
                destino_url = %s,
                tipo_destino = %s,
                timeline_json = %s,
                campos_extra = %s
            WHERE id = %s
        """, (
            request.form.get('titulo', ''),
            request.form.get('mensagem', ''),
            foto_url,
            template_slug,
            request.form.get('destino_url') or None,
            request.form.get('tipo_destino', pagina.get('tipo_destino')),
            timeline_json,
            campos_extra_json,
            pagina['id'],
        ))

        flash('Página atualizada com sucesso!', 'success')
        return redirect(f'/{slug}/painel')

    total_scans = query_one("SELECT COUNT(*) as total FROM brindes_scans WHERE pagina_id = %s", (pagina['id'],))
    pagina_atualizada, carimbos_atual = montar_page_context(get_pagina_by_slug(slug))
    campos_por_template = {t['slug']: t.get('campos', []) for t in templates_disponiveis}

    return render_template(
        'painel.html',
        pagina=pagina_atualizada,
        carimbos_atual=carimbos_atual,
        total_scans=total_scans['total'] if total_scans else 0,
        qr_url=f"/{slug}/qr.png",
        templates=templates_disponiveis,
        campos_por_template=campos_por_template,
        anuncio_topo=get_anuncio('topo', contexto='funcionalidade'),
        base_url=BASE_URL,
        fidelize_base_url=FIDELIZE_BASE_URL,
    )


# --- CARTÃO FIDELIDADE: somar/zerar carimbo (protegido, mesma sessão do painel) ---
@app.route('/<slug>/carimbar', methods=['POST'])
def carimbar_pagina(slug):
    pagina = get_pagina_by_slug(slug)
    if not pagina:
        abort(404)
    if not pode_gerenciar_pagina(pagina):
        if pagina.get('conta_id'):
            session['fidelize_redirect_apos_login'] = f'{BASE_URL}/{slug}/painel'
            flash('Faça login na sua conta Fidelize pra gerenciar esse QR code.', 'error')
            return redirect(f'{FIDELIZE_BASE_URL}/login')
        return redirect(f'/{slug}/login')

    campos_extra = pagina.get('campos_extra') or {}
    if isinstance(campos_extra, str):
        try:
            campos_extra = json.loads(campos_extra)
        except (ValueError, TypeError):
            campos_extra = {}
    atual = campos_extra.get('carimbos_atual', 0) or 0
    try:
        atual = int(atual)
    except (ValueError, TypeError):
        atual = 0

    acao = request.form.get('acao', 'somar')
    if acao == 'zerar':
        atual = 0
    else:
        atual += 1
    campos_extra['carimbos_atual'] = atual

    execute(
        "UPDATE brindes_paginas SET campos_extra = %s WHERE id = %s",
        (psycopg2.extras.Json(campos_extra), pagina['id']),
    )
    flash('Carimbo zerado.' if acao == 'zerar' else 'Carimbo adicionado!', 'success')
    return redirect(f'/{slug}/painel')


# =====================================================================
#  CATÁLOGO E DIRETÓRIO — páginas públicas
# =====================================================================

@app.route('/ocasiao/<slug>')
def ocasiao_publica(slug):
    ocasiao = query_one("SELECT * FROM brindes_ocasioes WHERE slug = %s AND ativo = TRUE", (slug,))
    if not ocasiao:
        abort(404)

    # Exibição do tipo de impressão migrada pra M2M (brindes_brinde_tipos_impressao),
    # agregado como string separada por vírgula pra manter compatibilidade com
    # ocasiao.html (que espera brinde.tipo_nome como um valor só). Só esse ponto
    # da rota foi ajustado — o resto continua igual.
    brindes = query_all("""
        SELECT b.*, string_agg(t.nome, ', ' ORDER BY t.nome) as tipo_nome
        FROM brindes_brindes b
        LEFT JOIN brindes_brinde_tipos_impressao bti ON bti.brinde_id = b.id
        LEFT JOIN brindes_tipos_impressao t ON t.id = bti.tipo_impressao_id
        WHERE b.ocasiao_id = %s AND b.ativo = TRUE
        GROUP BY b.id
        ORDER BY b.nome
    """, (ocasiao['id'],))

    return render_template(
        'ocasiao.html',
        ocasiao=ocasiao, brindes=brindes,
        anuncio_topo=get_anuncio('topo', ocasiao['id']),
        anuncio_meio=get_anuncio('meio', ocasiao['id']),
    )


@app.route('/brinde/<slug>')
def brinde_publico(slug):
    brinde = query_one("""
        SELECT b.*, o.nome as ocasiao_nome, o.slug as ocasiao_slug
        FROM brindes_brindes b
        LEFT JOIN brindes_ocasioes o ON o.id = b.ocasiao_id
        WHERE b.slug = %s AND b.ativo = TRUE
    """, (slug,))
    if not brinde:
        abort(404)

    # Um brinde pode ter vários tipos de impressão agora (brindes_brinde_tipos_impressao).
    tipos_impressao = query_all("""
        SELECT t.id, t.nome, t.slug
        FROM brindes_brinde_tipos_impressao bti
        JOIN brindes_tipos_impressao t ON t.id = bti.tipo_impressao_id
        WHERE bti.brinde_id = %s AND t.ativo = TRUE
        ORDER BY t.nome
    """, (brinde['id'],))

    return render_template(
        'brinde.html',
        brinde=brinde,
        tipos_impressao=tipos_impressao,
        anuncio_topo=get_anuncio('topo', brinde.get('ocasiao_id')),
        anuncio_meio=get_anuncio('meio', brinde.get('ocasiao_id')),
    )


@app.route('/brinde/<slug>/orcamento', methods=['POST'])
def brinde_orcamento(slug):
    if not check_limit(f"orcamento_{get_ip()}", 5, 300):
        flash("Muitas tentativas. Aguarde um pouco.", "error")
        return redirect(f'/brinde/{slug}')

    brinde = query_one("SELECT * FROM brindes_brindes WHERE slug = %s AND ativo = TRUE", (slug,))
    if not brinde:
        abort(404)

    nome = request.form.get('nome', '').strip()
    email = request.form.get('email', '').strip()
    telefone = request.form.get('telefone', '').strip()
    mensagem = request.form.get('mensagem', '').strip()

    if not nome or (not email and not telefone):
        flash('Preencha seu nome e pelo menos um contato (e-mail ou telefone).', 'error')
        return redirect(f'/brinde/{slug}')

    empresas_destaque = []
    if brinde['ocasiao_id']:
        empresas_destaque = query_all("""
            SELECT e.id FROM brindes_empresas e
            JOIN brindes_empresa_ocasioes eo ON eo.empresa_id = e.id
            WHERE eo.ocasiao_id = %s AND e.plano = 'destaque' AND e.ativo = TRUE
        """, (brinde['ocasiao_id'],))

    if empresas_destaque:
        for emp in empresas_destaque:
            execute("""
                INSERT INTO brindes_leads (brinde_id, empresa_id, nome, email, telefone, mensagem, valor_taxa, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'pendente')
            """, (brinde['id'], emp['id'], nome, email or None, telefone or None, mensagem or None, TAXA_LEAD_PADRAO))
    else:
        # Nenhuma empresa em destaque pra essa ocasião ainda — cai pro admin encaminhar manualmente
        execute("""
            INSERT INTO brindes_leads (brinde_id, empresa_id, nome, email, telefone, mensagem, valor_taxa, status)
            VALUES (%s, NULL, %s, %s, %s, %s, %s, 'pendente')
        """, (brinde['id'], nome, email or None, telefone or None, mensagem or None, TAXA_LEAD_PADRAO))

    flash('Pedido de orçamento enviado! Você vai ser contatado em breve.', 'success')
    return redirect(f'/brinde/{slug}')


@app.route('/diretorio')
def diretorio_publico():
    empresas = query_all("""
        SELECT * FROM brindes_empresas
        WHERE ativo = TRUE
        ORDER BY (plano = 'destaque') DESC, nome
    """)
    return render_template(
        'diretorio.html',
        empresas=empresas,
        anuncio_topo=get_anuncio('topo'),
        anuncio_meio=get_anuncio('meio'),
    )


# --- PÁGINA INDIVIDUAL DE EMPRESA (Parte 1) ---
@app.route('/empresa/<slug>')
def empresa_publica(slug):
    empresa = query_one("SELECT * FROM brindes_empresas WHERE slug = %s AND ativo = TRUE", (slug,))
    if not empresa:
        abort(404)

    # Proteção destaque: empresa no plano 'destaque' pagou pra ter a própria
    # página sem publicidade de terceiros — nunca busca/mostra anúncio de
    # outra empresa aqui, ponto final. Empresas no plano grátis continuam
    # podendo receber anúncio genérico ou segmentado pela cidade delas.
    anuncio_topo = None
    if empresa.get('plano') != 'destaque':
        anuncio_topo = get_anuncio('topo', cidade_slug=empresa.get('cidade_slug'))

    return render_template('negocio_brindes.html', empresa=empresa,
                            anuncio_topo=anuncio_topo,
                            current_year=datetime.now().year)


# --- PÁGINA POR TIPO DE IMPRESSÃO (Parte 2) ---
@app.route('/impressao/<slug>')
def impressao_publica(slug):
    tipo = query_one("SELECT * FROM brindes_tipos_impressao WHERE slug = %s AND ativo = TRUE", (slug,))
    if not tipo:
        abort(404)
    # Um brinde pode ter vários tipos de impressão agora — usa a tabela M2M
    # brindes_brinde_tipos_impressao em vez da coluna legada b.tipo_impressao_id.
    brindes = query_all("""
        SELECT b.*, o.nome as ocasiao_nome, o.slug as ocasiao_slug
        FROM brindes_brindes b
        JOIN brindes_brinde_tipos_impressao bti ON bti.brinde_id = b.id
        LEFT JOIN brindes_ocasioes o ON o.id = b.ocasiao_id
        WHERE bti.tipo_impressao_id = %s AND b.ativo = TRUE
        ORDER BY b.created_at DESC
    """, (tipo['id'],))
    return render_template('impressao.html', tipo=tipo, brindes=brindes,
                            anuncio_topo=get_anuncio('topo', tipo_impressao_id=tipo['id']),
                            current_year=datetime.now().year)


# =====================================================================
#  API PÚBLICA (JSON) — alimenta o padrão de chip + grid da home
# =====================================================================

@app.route('/api/brindes')
def api_brindes():
    brindes = query_all("""
        SELECT b.id, b.nome, b.slug, b.imagem_url,
               o.nome as ocasiao_nome, o.slug as ocasiao_slug,
               COALESCE(array_agg(t.nome) FILTER (WHERE t.nome IS NOT NULL), '{}') as tipos_nomes,
               COALESCE(array_agg(t.slug) FILTER (WHERE t.slug IS NOT NULL), '{}') as tipos_slugs
        FROM brindes_brindes b
        LEFT JOIN brindes_ocasioes o ON o.id = b.ocasiao_id
        LEFT JOIN brindes_brinde_tipos_impressao bti ON bti.brinde_id = b.id
        LEFT JOIN brindes_tipos_impressao t ON t.id = bti.tipo_impressao_id AND t.ativo = TRUE
        WHERE b.ativo = TRUE
        GROUP BY b.id, o.nome, o.slug
        ORDER BY b.created_at DESC
    """)
    return jsonify(brindes)


@app.route('/api/empresas')
def api_empresas():
    empresas = query_all("""
        SELECT id, nome, slug, logo_url, cidade, cidade_slug, plano
        FROM brindes_empresas
        WHERE ativo = TRUE
        ORDER BY (plano = 'destaque') DESC, RANDOM()
    """)
    return jsonify(empresas)


@app.route('/api/tipos-impressao')
def api_tipos_impressao():
    tipos = query_all("""
        SELECT id, nome, slug, imagem_url
        FROM brindes_tipos_impressao
        WHERE ativo = TRUE
        ORDER BY nome
    """)
    return jsonify(tipos)


# --- CADASTRO PÚBLICO DE EMPRESA ---
@app.route('/cadastrar-empresa', methods=['POST'])
def cadastrar_empresa_publico():
    if not check_limit(f"cad_empresa_{get_ip()}", 5, 3600):
        return jsonify({'ok': False, 'erro': 'Muitas tentativas. Tente mais tarde.'}), 429

    nome     = request.form.get('nome', '').strip()
    cidade   = request.form.get('cidade', '').strip()
    bairro   = request.form.get('bairro', '').strip()
    whatsapp = request.form.get('whatsapp', '').strip()
    telefone = request.form.get('telefone', '').strip()
    instagram= request.form.get('instagram', '').strip()
    site_url = request.form.get('site_url', '').strip()
    descricao= request.form.get('descricao', '').strip()
    logo_url = request.form.get('logo_url', '').strip()

    if not nome:
        return jsonify({'ok': False, 'erro': 'Nome da empresa é obrigatório.'}), 400

    # Monta slug único com sufixo aleatório para evitar conflito
    import random, string
    base_slug = slugify_cidade(nome) or 'empresa'
    sufixo    = ''.join(random.choices(string.ascii_lowercase + string.digits, k=5))
    slug      = f"{base_slug}-{sufixo}"

    cidade_slug = slugify_cidade(cidade) if cidade else ''

    # Adiciona bairro à descrição se preenchido
    desc_final = descricao
    if bairro:
        desc_final = f"📍 {bairro}\n\n{desc_final}".strip()
    if instagram:
        desc_final = f"{desc_final}\n\n📸 {instagram}".strip()

    try:
        execute("""
            INSERT INTO brindes_empresas
                (nome, slug, telefone, whatsapp, site, cidade, cidade_slug, descricao, logo_url, plano, ativo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'gratis', FALSE)
        """, (
            nome, slug,
            telefone or None, whatsapp or None, site_url or None,
            cidade or None, cidade_slug,
            desc_final or None, logo_url or None,
        ))
        return jsonify({'ok': True})
    except Exception as ex:
        return jsonify({'ok': False, 'erro': 'Erro ao salvar. Tente novamente.'}), 500


# --- PÁGINA POR CIDADE (Parte 3) ---
@app.route('/cidade/<slug>')
def cidade_publica(slug):
    empresas = query_all("""
        SELECT * FROM brindes_empresas
        WHERE cidade_slug = %s AND ativo = TRUE
        ORDER BY (plano = 'destaque') DESC, nome
    """, (slug,))
    if not empresas:
        abort(404)
    return render_template('cidade.html', cidade_nome=empresas[0]['cidade'],
                            empresas=empresas, anuncio_topo=get_anuncio('topo', cidade_slug=slug),
                            current_year=datetime.now().year)


# =====================================================================
#  ADMIN — painel global (ocasiões, tipos, brindes, empresas, leads)
# =====================================================================

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if not check_limit(f"admin_login_{get_ip()}", 10, 60):
        flash("Muitas tentativas. Aguarde.", "error")
        return render_template('admin_login.html')

    if request.method == 'POST':
        email = request.form.get('email', '').strip()
        senha = request.form.get('senha', '')
        admin = query_one("SELECT * FROM brindes_admin WHERE email = %s", (email,))
        if admin and check_password_hash(admin['password_hash'], senha):
            session['admin_id'] = admin['id']
            return redirect('/admin')
        flash('E-mail ou senha incorretos.', 'error')

    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    session.pop('admin_id', None)
    return redirect('/admin/login')


def _admin_contexto_base():
    """Contexto comum usado pelo admin.html em qualquer seção (dashboard,
    ocasiões, tipos, brindes, empresas, leads, páginas, templates)."""
    stats = {
        'total_paginas': query_one("SELECT COUNT(*) as c FROM brindes_paginas")['c'],
        'total_scans': query_one("SELECT COUNT(*) as c FROM brindes_scans")['c'],
        'total_brindes': query_one("SELECT COUNT(*) as c FROM brindes_brindes")['c'],
        'total_empresas': query_one("SELECT COUNT(*) as c FROM brindes_empresas WHERE ativo = TRUE")['c'],
        'empresas_pendentes': query_one("SELECT COUNT(*) as c FROM brindes_empresas WHERE ativo = FALSE")['c'],
        'leads_pendentes': query_one("SELECT COUNT(*) as c FROM brindes_leads WHERE status = 'pendente'")['c'],
        'paginas_pendentes': query_one("SELECT COUNT(*) as c FROM brindes_paginas WHERE plano = 'pendente' AND ativo = FALSE")['c'],
        'fidelize_aguardando': query_one("""
            SELECT COUNT(*) as c FROM brindes_fidelidade_contas
            WHERE ativo = TRUE AND mensalidade_ativa = FALSE AND qr_codes_disponiveis = 0
        """)['c'],
    }
    ocasioes = query_all("SELECT * FROM brindes_ocasioes ORDER BY nome")
    tipos = query_all("SELECT * FROM brindes_tipos_impressao ORDER BY nome")
    # Um brinde pode ter vários tipos de impressão agora — agrega via a tabela
    # M2M brindes_brinde_tipos_impressao em vez da coluna legada b.tipo_impressao_id.
    brindes = query_all("""
        SELECT b.*, o.nome as ocasiao_nome,
               COALESCE(array_agg(t.nome) FILTER (WHERE t.nome IS NOT NULL), '{}') as tipos_nomes,
               COALESCE(array_agg(t.id) FILTER (WHERE t.id IS NOT NULL), '{}') as tipos_ids
        FROM brindes_brindes b
        LEFT JOIN brindes_ocasioes o ON o.id = b.ocasiao_id
        LEFT JOIN brindes_brinde_tipos_impressao bti ON bti.brinde_id = b.id
        LEFT JOIN brindes_tipos_impressao t ON t.id = bti.tipo_impressao_id
        GROUP BY b.id, o.nome
        ORDER BY b.created_at DESC
    """)
    empresas = query_all("SELECT * FROM brindes_empresas WHERE ativo = TRUE ORDER BY nome")
    empresas_pendentes = query_all("SELECT * FROM brindes_empresas WHERE ativo = FALSE ORDER BY created_at DESC")
    leads = query_all("""
        SELECT l.*, b.nome as brinde_nome, e.nome as empresa_nome
        FROM brindes_leads l
        LEFT JOIN brindes_brindes b ON b.id = l.brinde_id
        LEFT JOIN brindes_empresas e ON e.id = l.empresa_id
        ORDER BY l.created_at DESC
    """)
    paginas = query_all("""
        SELECT p.*, COUNT(s.id) as total_scans
        FROM brindes_paginas p
        LEFT JOIN brindes_scans s ON s.pagina_id = p.id
        GROUP BY p.id
        ORDER BY p.created_at DESC
    """)
    templates = carregar_templates()
    templates_por_slug = {t['slug']: t for t in templates}
    paginas_pendentes = query_all("""
        SELECT id, slug, email, titulo, template, created_at
        FROM brindes_paginas
        WHERE plano = 'pendente' AND ativo = FALSE
        ORDER BY created_at DESC
    """)
    # Anexa nome/preço do template escolhido em cada pendente (vem do .json,
    # não do banco), pra mostrar no card do admin sem precisar de outro join.
    for p in paginas_pendentes:
        tpl = templates_por_slug.get(p['template'])
        p['template_nome'] = tpl['nome'] if tpl else p['template']
        p['template_preco'] = tpl.get('preco') if tpl else None
    # Só os marcados "exclusivo": true no .json — usados pra popular o
    # dropdown de "liberar template" na tabela de contas do Fidelize.
    templates_exclusivos = [t for t in templates if t.get('exclusivo')]
    contas_fidelize = query_all("""
        SELECT c.*, COUNT(p.id) as total_qrs_criados
        FROM brindes_fidelidade_contas c
        LEFT JOIN brindes_paginas p ON p.conta_id = c.id
        GROUP BY c.id
        ORDER BY c.criado_em DESC
    """)
    # Cidades distintas com slug preenchido, pra popular o select de escopo
    # de anúncio por cidade (usa as mesmas cidades já cadastradas em empresas).
    cidades = query_all("""
        SELECT DISTINCT cidade, cidade_slug FROM brindes_empresas
        WHERE cidade_slug IS NOT NULL AND cidade_slug != ''
        ORDER BY cidade
    """)

    return dict(
        stats=stats, ocasioes=ocasioes, tipos=tipos,
        brindes=brindes, empresas=empresas, empresas_pendentes=empresas_pendentes,
        leads=leads, paginas=paginas, templates=templates, cidades=cidades,
        contas_fidelize=contas_fidelize, templates_exclusivos=templates_exclusivos,
    )


@app.route('/admin')
@admin_required
def admin_dashboard():
    return render_template('admin.html', secao_ativa='dashboard', **_admin_contexto_base())


# --- TEMPLATES (Parte 3 — só leitura + ativar/desativar) ---
@app.route('/admin/templates')
@admin_required
def admin_templates():
    return render_template('admin.html', secao_ativa='templates', **_admin_contexto_base())


@app.route('/admin/templates/<slug>/toggle', methods=['POST'])
@admin_required
def admin_templates_toggle(slug):
    tpl = get_template_por_slug(slug)
    if not tpl:
        abort(404)
    meta_path = os.path.join(app.template_folder, 'paginas', f'{slug}.json')
    tpl['ativo'] = not tpl.get('ativo', True)
    # slug/arquivo são derivados do nome do arquivo, não fazem parte do metadado salvo em disco
    tpl.pop('slug', None)
    tpl.pop('arquivo', None)
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(tpl, f, ensure_ascii=False, indent=2)
    return redirect('/admin/templates')


@app.route('/admin/templates/<slug>/editar', methods=['GET', 'POST'])
@admin_required
def admin_templates_editar(slug):
    tpl = get_template_por_slug(slug)
    if not tpl:
        abort(404)
    meta_path = os.path.join(app.template_folder, 'paginas', f'{slug}.json')

    # GET: o drawer do admin busca os dados atuais pra preencher o formulário.
    if request.method == 'GET':
        return jsonify(tpl)

    # POST: salva nome, preview_url, tier e preco. "campos" e "demo" não são
    # tocados aqui — continuam só editáveis por arquivo direto no .json.
    nome = request.form.get('nome', tpl.get('nome', '')).strip()
    preview_url = request.form.get('preview_url', '').strip()
    tier = request.form.get('tier', 'gratis').strip()
    if tier not in ('gratis', 'pago'):
        tier = 'gratis'
    preco_raw = request.form.get('preco', '0').strip()
    try:
        preco = float(preco_raw) if preco_raw else 0
    except ValueError:
        preco = 0
    if tier == 'gratis':
        preco = 0

    if not nome:
        return jsonify({'erro': 'Preencha o nome do template.'}), 400

    tpl['nome'] = nome
    tpl['preview_url'] = preview_url
    tpl['tier'] = tier
    tpl['preco'] = preco
    # slug/arquivo são derivados do nome do arquivo, não fazem parte do metadado salvo em disco
    tpl.pop('slug', None)
    tpl.pop('arquivo', None)
    try:
        with open(meta_path, 'w', encoding='utf-8') as f:
            json.dump(tpl, f, ensure_ascii=False, indent=2)
    except Exception:
        return jsonify({'erro': 'Erro ao salvar o template.'}), 400

    # A URL da capa também vai pro banco (UPSERT), porque o .json acima
    # some a cada redeploy — o banco não. carregar_templates() dá
    # prioridade a esse valor salvo aqui.
    try:
        execute("""
            INSERT INTO brindes_templates_capas (slug, preview_url, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (slug) DO UPDATE
                SET preview_url = EXCLUDED.preview_url, updated_at = NOW();
        """, (slug, preview_url))
    except Exception:
        return jsonify({'erro': 'Template salvo, mas a URL da capa não persistiu no banco.'}), 400

    return jsonify({'ok': True})


# --- PÁGINAS (usuários que geraram QR code) ---
@app.route('/admin/paginas/<int:item_id>/editar', methods=['POST'])
@admin_required
def admin_paginas_editar(item_id):
    novo_slug = request.form.get('slug', '').strip().lower()
    novo_email = request.form.get('email', '').strip()

    if not novo_slug:
        flash('O link (slug) não pode ficar vazio.', 'error')
        return redirect('/admin#paginas')

    conflito = query_one(
        "SELECT id FROM brindes_paginas WHERE slug = %s AND id != %s",
        (novo_slug, item_id)
    )
    if conflito:
        flash('Esse link já está em uso por outra página.', 'error')
        return redirect('/admin#paginas')

    try:
        execute(
            "UPDATE brindes_paginas SET slug = %s, email = %s WHERE id = %s",
            (novo_slug, novo_email or None, item_id)
        )
        flash('Página atualizada.', 'success')
    except Exception:
        flash('Erro ao atualizar a página.', 'error')
    return redirect('/admin#paginas')


@app.route('/admin/paginas/<int:item_id>/toggle', methods=['POST'])
@admin_required
def admin_paginas_toggle(item_id):
    execute("UPDATE brindes_paginas SET ativo = NOT ativo WHERE id = %s", (item_id,))
    return redirect('/admin#paginas')


@app.route('/admin/paginas/<int:item_id>/confirmar-pagamento', methods=['POST'])
@admin_required
def admin_paginas_confirmar_pagamento(item_id):
    """Confirma manualmente o pagamento de um template pago (Pix + comprovante
    conferido no WhatsApp) e libera a página: plano vira 'pago', ativo=TRUE."""
    pagina = query_one("SELECT id, slug FROM brindes_paginas WHERE id = %s AND plano = 'pendente'", (item_id,))
    if not pagina:
        flash('Página pendente não encontrada (talvez já tenha sido confirmada).', 'error')
        return redirect('/admin#paginas')

    execute(
        "UPDATE brindes_paginas SET plano = 'pago', ativo = TRUE WHERE id = %s",
        (item_id,)
    )
    flash(f'Pagamento confirmado — /{pagina["slug"]} está ativa.', 'success')
    return redirect('/admin#paginas')


@app.route('/admin/paginas/<int:item_id>/delete', methods=['POST'])
@admin_required
def admin_paginas_delete(item_id):
    execute("DELETE FROM brindes_paginas WHERE id = %s", (item_id,))
    flash('Página excluída definitivamente.', 'success')
    return redirect('/admin#paginas')


@app.route('/admin/paginas/<int:item_id>/resetar-senha', methods=['POST'])
@admin_required
def admin_paginas_resetar_senha(item_id):
    pagina = query_one("SELECT * FROM brindes_paginas WHERE id = %s", (item_id,))
    if not pagina:
        flash('Página não encontrada.', 'error')
        return redirect('/admin#paginas')

    senha_nova = gerar_senha_aleatoria()
    execute(
        "UPDATE brindes_paginas SET senha_hash = %s WHERE id = %s",
        (generate_password_hash(senha_nova), item_id)
    )

    if not pagina.get('email'):
        flash(
            f'Senha resetada, mas essa página não tem e-mail cadastrado pra enviar. '
            f'Nova senha (anote agora, não vai aparecer de novo): {senha_nova}',
            'error'
        )
        return redirect('/admin#paginas')

    link_login = f"{BASE_URL}/{pagina['slug']}/login"
    corpo = (
        f"Olá!\n\n"
        f"Sua senha de acesso ao painel do QRCodeBrindes foi redefinida.\n\n"
        f"Link do seu QR code: {BASE_URL}/{pagina['slug']}\n"
        f"Link de login: {link_login}\n"
        f"Nova senha: {senha_nova}\n\n"
        f"Recomendamos trocar essa senha assim que possível (ainda não temos tela de troca de senha "
        f"pelo próprio painel — fale com a gente se precisar trocar de novo).\n\n"
        f"— QRCodeBrindes"
    )

    sucesso, erro = enviar_email(
        pagina['email'],
        "Sua senha do QRCodeBrindes foi redefinida",
        corpo
    )

    if sucesso:
        flash(f"Senha resetada e enviada por e-mail para {pagina['email']}.", 'success')
    else:
        flash(
            f"Senha resetada, mas houve erro ao enviar o e-mail ({erro}). "
            f"Nova senha (anote agora, não vai aparecer de novo): {senha_nova}",
            'error'
        )

    return redirect('/admin#paginas')


# --- OCASIÕES ---
@app.route('/admin/ocasioes/add', methods=['POST'])
@admin_required
def admin_ocasioes_add():
    nome = request.form.get('nome', '').strip()
    slug = request.form.get('slug', '').strip().lower()
    sazonal = 'sazonal' in request.form
    if nome and slug:
        try:
            execute("INSERT INTO brindes_ocasioes (nome, slug, sazonal) VALUES (%s, %s, %s)", (nome, slug, sazonal))
            flash('Ocasião adicionada.', 'success')
        except Exception:
            flash('Erro ao adicionar (slug já existe?).', 'error')
    return redirect('/admin#ocasioes')


@app.route('/admin/ocasioes/<int:item_id>/editar', methods=['GET', 'POST'])
@admin_required
def admin_ocasioes_editar(item_id):
    ocasiao = query_one("SELECT * FROM brindes_ocasioes WHERE id = %s", (item_id,))
    if not ocasiao:
        abort(404)

    if request.method == 'GET':
        return jsonify(ocasiao)

    nome = request.form.get('nome', '').strip()
    slug = request.form.get('slug', '').strip().lower()
    descricao = request.form.get('descricao', '').strip()
    imagem_url = request.form.get('imagem_url', '').strip()
    sazonal = 'sazonal' in request.form

    if not nome or not slug:
        return jsonify({'erro': 'Preencha nome e slug.'}), 400

    try:
        execute("""
            UPDATE brindes_ocasioes
            SET nome = %s, slug = %s, descricao = %s, imagem_url = %s, sazonal = %s
            WHERE id = %s
        """, (nome, slug, descricao or None, imagem_url or None, sazonal, item_id))
        return jsonify({'ok': True})
    except Exception:
        return jsonify({'erro': 'Erro ao salvar (slug já existe?).'}), 400


@app.route('/admin/ocasioes/<int:item_id>/toggle', methods=['POST'])
@admin_required
def admin_ocasioes_toggle(item_id):
    execute("UPDATE brindes_ocasioes SET ativo = NOT ativo WHERE id = %s", (item_id,))
    return redirect('/admin#ocasioes')


@app.route('/admin/ocasioes/<int:item_id>/delete', methods=['POST'])
@admin_required
def admin_ocasioes_delete(item_id):
    execute("DELETE FROM brindes_ocasioes WHERE id = %s", (item_id,))
    return redirect('/admin#ocasioes')


# --- TIPOS DE IMPRESSÃO ---
@app.route('/admin/tipos-impressao/add', methods=['POST'])
@admin_required
def admin_tipos_add():
    nome = request.form.get('nome', '').strip()
    slug = request.form.get('slug', '').strip().lower()
    imagem_url = request.form.get('imagem_url', '').strip()
    if nome and slug:
        try:
            execute(
                "INSERT INTO brindes_tipos_impressao (nome, slug, imagem_url) VALUES (%s, %s, %s)",
                (nome, slug, imagem_url or None)
            )
            flash('Tipo de impressão adicionado.', 'success')
        except Exception:
            flash('Erro ao adicionar (slug já existe?).', 'error')
    return redirect('/admin#tipos')


@app.route('/admin/tipos-impressao/<int:item_id>/editar', methods=['GET', 'POST'])
@admin_required
def admin_tipos_editar(item_id):
    tipo = query_one("SELECT * FROM brindes_tipos_impressao WHERE id = %s", (item_id,))
    if not tipo:
        abort(404)

    if request.method == 'GET':
        return jsonify(tipo)

    nome = request.form.get('nome', '').strip()
    slug = request.form.get('slug', '').strip().lower()
    descricao = request.form.get('descricao', '').strip()
    imagem_url = request.form.get('imagem_url', '').strip()

    if not nome or not slug:
        return jsonify({'erro': 'Preencha nome e slug.'}), 400

    try:
        execute("""
            UPDATE brindes_tipos_impressao
            SET nome = %s, slug = %s, descricao = %s, imagem_url = %s
            WHERE id = %s
        """, (nome, slug, descricao or None, imagem_url or None, item_id))
        return jsonify({'ok': True})
    except Exception:
        return jsonify({'erro': 'Erro ao salvar (slug já existe?).'}), 400


@app.route('/admin/tipos-impressao/<int:item_id>/toggle', methods=['POST'])
@admin_required
def admin_tipos_toggle(item_id):
    execute("UPDATE brindes_tipos_impressao SET ativo = NOT ativo WHERE id = %s", (item_id,))
    return redirect('/admin#tipos')


@app.route('/admin/tipos-impressao/<int:item_id>/delete', methods=['POST'])
@admin_required
def admin_tipos_delete(item_id):
    execute("DELETE FROM brindes_tipos_impressao WHERE id = %s", (item_id,))
    return redirect('/admin#tipos')


# --- BRINDES (catálogo) ---
@app.route('/admin/brindes/add', methods=['POST'])
@admin_required
def admin_brindes_add():
    nome = request.form.get('nome', '').strip()
    slug = request.form.get('slug', '').strip().lower()
    descricao = request.form.get('descricao', '')
    ocasiao_id = request.form.get('ocasiao_id') or None
    # Um brinde pode ter vários tipos de impressão agora (checkboxes/multi-select).
    # A coluna legada tipo_impressao_id não é mais preenchida — a fonte de
    # verdade passa a ser brindes_brinde_tipos_impressao.
    tipo_impressao_ids = [int(v) for v in request.form.getlist('tipo_impressao_ids') if v]

    imagem_url = None
    f = request.files.get('imagem')
    if f and f.filename:
        imagem_url = upload_imgur(f)

    if nome and slug:
        try:
            novo_id = execute_returning("""
                INSERT INTO brindes_brindes (nome, slug, descricao, ocasiao_id, imagem_url)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (nome, slug, descricao, ocasiao_id, imagem_url))
            sync_brinde_tipos_impressao(novo_id, tipo_impressao_ids)
            flash('Brinde adicionado.', 'success')
        except Exception:
            flash('Erro ao adicionar (slug já existe?).', 'error')
    return redirect('/admin#brindes')


@app.route('/admin/brindes/<int:item_id>/editar', methods=['GET', 'POST'])
@admin_required
def admin_brindes_editar(item_id):
    brinde = query_one("SELECT * FROM brindes_brindes WHERE id = %s", (item_id,))
    if not brinde:
        abort(404)

    if request.method == 'GET':
        brinde['tipo_impressao_ids'] = get_tipos_impressao_ids(item_id)
        return jsonify(brinde)

    nome = request.form.get('nome', '').strip()
    slug = request.form.get('slug', '').strip().lower()
    descricao = request.form.get('descricao', '').strip()
    imagem_url = request.form.get('imagem_url', '').strip()
    ocasiao_id = request.form.get('ocasiao_id') or None
    # Vários tipos de impressão por brinde — substitui as relações antigas
    # em brindes_brinde_tipos_impressao. A coluna legada tipo_impressao_id
    # não é mais escrita (não é mais a fonte de verdade).
    tipo_impressao_ids = [int(v) for v in request.form.getlist('tipo_impressao_ids') if v]

    if not nome or not slug:
        return jsonify({'erro': 'Preencha nome e slug.'}), 400

    try:
        execute("""
            UPDATE brindes_brindes
            SET nome = %s, slug = %s, descricao = %s, imagem_url = %s,
                ocasiao_id = %s
            WHERE id = %s
        """, (nome, slug, descricao or None, imagem_url or None, ocasiao_id, item_id))
        sync_brinde_tipos_impressao(item_id, tipo_impressao_ids)
        return jsonify({'ok': True})
    except Exception:
        return jsonify({'erro': 'Erro ao salvar (slug já existe?).'}), 400


@app.route('/admin/brindes/<int:item_id>/toggle', methods=['POST'])
@admin_required
def admin_brindes_toggle(item_id):
    execute("UPDATE brindes_brindes SET ativo = NOT ativo WHERE id = %s", (item_id,))
    return redirect('/admin#brindes')


@app.route('/admin/brindes/<int:item_id>/delete', methods=['POST'])
@admin_required
def admin_brindes_delete(item_id):
    execute("DELETE FROM brindes_brindes WHERE id = %s", (item_id,))
    return redirect('/admin#brindes')



@app.route("/admin/empresas/<int:item_id>/aprovar", methods=["POST"])
@admin_required
def admin_empresas_aprovar(item_id):
    execute("UPDATE brindes_empresas SET ativo = TRUE WHERE id = %s", (item_id,))
    flash("Empresa aprovada e publicada no diretório.", "success")
    return redirect("/admin#empresas")

# --- EMPRESAS (diretório) ---
@app.route('/admin/empresas/add', methods=['POST'])
@admin_required
def admin_empresas_add():
    nome = request.form.get('nome', '').strip()
    slug = request.form.get('slug', '').strip().lower()
    email = request.form.get('email', '').strip()
    telefone = request.form.get('telefone', '').strip()
    whatsapp = request.form.get('whatsapp', '').strip()
    site = request.form.get('site', '').strip()
    cidade = request.form.get('cidade', '').strip()
    descricao = request.form.get('descricao', '').strip()
    logo_url = request.form.get('logo_url', '').strip()
    plano = request.form.get('plano', 'gratis')
    cidade_slug = slugify_cidade(cidade)

    if nome and slug:
        try:
            execute("""
                INSERT INTO brindes_empresas
                    (nome, slug, email, telefone, whatsapp, site, cidade, cidade_slug, descricao, logo_url, plano)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                nome, slug, email or None, telefone or None, whatsapp or None,
                site or None, cidade or None, cidade_slug, descricao or None,
                logo_url or None, plano,
            ))
            flash('Empresa adicionada.', 'success')
        except Exception:
            flash('Erro ao adicionar (slug já existe?).', 'error')
    return redirect('/admin#empresas')


@app.route('/admin/empresas/<int:item_id>/editar', methods=['GET', 'POST'])
@admin_required
def admin_empresas_editar(item_id):
    empresa = query_one("SELECT * FROM brindes_empresas WHERE id = %s", (item_id,))
    if not empresa:
        abort(404)

    if request.method == 'GET':
        return jsonify(empresa)

    nome = request.form.get('nome', '').strip()
    slug = request.form.get('slug', '').strip().lower()
    email = request.form.get('email', '').strip()
    telefone = request.form.get('telefone', '').strip()
    whatsapp = request.form.get('whatsapp', '').strip()
    site = request.form.get('site', '').strip()
    cidade = request.form.get('cidade', '').strip()
    descricao = request.form.get('descricao', '').strip()
    logo_url = request.form.get('logo_url', '').strip()
    plano = request.form.get('plano', 'gratis')
    cidade_slug = slugify_cidade(cidade)

    if not nome or not slug:
        return jsonify({'erro': 'Preencha nome e slug.'}), 400

    try:
        execute("""
            UPDATE brindes_empresas
            SET nome = %s, slug = %s, email = %s, telefone = %s, whatsapp = %s,
                site = %s, cidade = %s, cidade_slug = %s, descricao = %s,
                logo_url = %s, plano = %s
            WHERE id = %s
        """, (
            nome, slug, email or None, telefone or None, whatsapp or None,
            site or None, cidade or None, cidade_slug, descricao or None,
            logo_url or None, plano, item_id,
        ))
        return jsonify({'ok': True})
    except Exception:
        return jsonify({'erro': 'Erro ao salvar (slug já existe?).'}), 400


@app.route('/admin/empresas/<int:item_id>/toggle', methods=['POST'])
@admin_required
def admin_empresas_toggle(item_id):
    execute("UPDATE brindes_empresas SET ativo = NOT ativo WHERE id = %s", (item_id,))
    return redirect('/admin#empresas')


@app.route('/admin/empresas/<int:item_id>/toggle-plano', methods=['POST'])
@admin_required
def admin_empresas_toggle_plano(item_id):
    empresa = query_one("SELECT plano FROM brindes_empresas WHERE id = %s", (item_id,))
    if empresa:
        novo_plano = 'gratis' if empresa['plano'] == 'destaque' else 'destaque'
        execute("UPDATE brindes_empresas SET plano = %s WHERE id = %s", (novo_plano, item_id))
    return redirect('/admin#empresas')


@app.route('/admin/empresas/<int:item_id>/delete', methods=['POST'])
@admin_required
def admin_empresas_delete(item_id):
    execute("DELETE FROM brindes_empresas WHERE id = %s", (item_id,))
    return redirect('/admin#empresas')


# --- FIDELIZE (liberação manual de conta, mesmo padrão de "template pago":
# sem gateway integrado, admin confirma o pagamento fora do sistema e libera
# um pacote de QR codes na mão) ---
@app.route('/admin/fidelize/<int:conta_id>/liberar', methods=['POST'])
@admin_required
def admin_fidelize_liberar(conta_id):
    try:
        quantidade = int(request.form.get('quantidade', '0'))
    except (TypeError, ValueError):
        quantidade = 0

    if quantidade > 0:
        execute(
            "UPDATE brindes_fidelidade_contas SET qr_codes_disponiveis = qr_codes_disponiveis + %s WHERE id = %s",
            (quantidade, conta_id)
        )
        flash(f'{quantidade} QR code(s) liberado(s) com sucesso.', 'success')
    else:
        flash('Informe uma quantidade válida.', 'error')

    return redirect('/admin#fidelize')


@app.route('/admin/fidelize/<int:conta_id>/toggle-mensalidade', methods=['POST'])
@admin_required
def admin_fidelize_toggle_mensalidade(conta_id):
    conta = query_one("SELECT mensalidade_ativa FROM brindes_fidelidade_contas WHERE id = %s", (conta_id,))
    if conta:
        execute(
            "UPDATE brindes_fidelidade_contas SET mensalidade_ativa = %s WHERE id = %s",
            (not conta['mensalidade_ativa'], conta_id)
        )
    return redirect('/admin#fidelize')


@app.route('/admin/fidelize/<int:conta_id>/toggle', methods=['POST'])
@admin_required
def admin_fidelize_toggle(conta_id):
    """Ativa/bloqueia a conta inteira — conta bloqueada não consegue mais
    logar em fidelize.qrcodebrindes.com.br (checado em fidelize_login)."""
    conta = query_one("SELECT ativo FROM brindes_fidelidade_contas WHERE id = %s", (conta_id,))
    if conta:
        execute(
            "UPDATE brindes_fidelidade_contas SET ativo = %s WHERE id = %s",
            (not conta['ativo'], conta_id)
        )
    return redirect('/admin#fidelize')


@app.route('/admin/fidelize/<int:conta_id>/liberar-template', methods=['POST'])
@admin_required
def admin_fidelize_liberar_template(conta_id):
    """Adiciona um slug de template exclusivo à lista templates_extras da
    conta — é isso que faz o template vendido separadamente aparecer no
    dropdown dela em /painel. Valida que o slug realmente existe e está
    marcado "exclusivo" no .json, pra não liberar qualquer coisa por engano
    (template comum não precisa disso, já é liberado pra todo mundo)."""
    template_slug = (request.form.get('template_slug') or '').strip()
    template_meta = get_template_por_slug(template_slug)

    if not (template_meta and template_meta.get('exclusivo')):
        flash('Esse template não existe ou não é um template exclusivo.', 'error')
        return redirect('/admin#fidelize')

    conta = query_one("SELECT templates_extras FROM brindes_fidelidade_contas WHERE id = %s", (conta_id,))
    if not conta:
        flash('Conta não encontrada.', 'error')
        return redirect('/admin#fidelize')

    extras = conta.get('templates_extras') or []
    if isinstance(extras, str):
        try:
            extras = json.loads(extras)
        except (ValueError, TypeError):
            extras = []

    if template_slug not in extras:
        extras.append(template_slug)
        execute(
            "UPDATE brindes_fidelidade_contas SET templates_extras = %s WHERE id = %s",
            (psycopg2.extras.Json(extras), conta_id)
        )
        flash(f'Template "{template_slug}" liberado pra essa conta.', 'success')
    else:
        flash('Essa conta já tinha esse template liberado.', 'success')

    return redirect('/admin#fidelize')


@app.route('/admin/fidelize/<int:conta_id>/revogar-template', methods=['POST'])
@admin_required
def admin_fidelize_revogar_template(conta_id):
    """Remove um slug da lista templates_extras — a conta deixa de ver e
    conseguir usar esse template exclusivo. Não mexe nas páginas que já
    estavam usando ele (se ela já tinha aplicado, o cartão continua com
    aquele template até ela trocar manualmente pra outro)."""
    template_slug = (request.form.get('template_slug') or '').strip()
    conta = query_one("SELECT templates_extras FROM brindes_fidelidade_contas WHERE id = %s", (conta_id,))
    if not conta:
        flash('Conta não encontrada.', 'error')
        return redirect('/admin#fidelize')

    extras = conta.get('templates_extras') or []
    if isinstance(extras, str):
        try:
            extras = json.loads(extras)
        except (ValueError, TypeError):
            extras = []

    if template_slug in extras:
        extras.remove(template_slug)
        execute(
            "UPDATE brindes_fidelidade_contas SET templates_extras = %s WHERE id = %s",
            (psycopg2.extras.Json(extras), conta_id)
        )
        flash(f'Template "{template_slug}" revogado dessa conta.', 'success')

    return redirect('/admin#fidelize')


# --- LEADS ---
@app.route('/admin/leads/<int:item_id>/status', methods=['POST'])
@admin_required
def admin_leads_status(item_id):
    status = request.form.get('status', 'pendente')
    execute("UPDATE brindes_leads SET status = %s WHERE id = %s", (status, item_id))
    return redirect('/admin#leads')


# =====================================================================
#  ANÚNCIOS — admin, padrão SPA/JSON (aba com drawer, igual ao hub)
#  Diferente das outras abas (que são POST + redirect + flash), essa
#  aba conversa com o front-end via fetch/JSON, então as rotas abaixo
#  devolvem jsonify em vez de redirect.
# =====================================================================

@app.route('/admin/anuncios')
@admin_required
def admin_anuncios_listar():
    anuncios = query_all("""
        SELECT a.*, o.nome as ocasiao_nome, t.nome as tipo_impressao_nome
        FROM brindes_anuncios a
        LEFT JOIN brindes_ocasioes o ON o.id = a.ocasiao_id
        LEFT JOIN brindes_tipos_impressao t ON t.id = a.tipo_impressao_id
        ORDER BY a.created_at DESC
    """)
    return jsonify(anuncios)


@app.route('/admin/anuncios/novo', methods=['POST'])
@admin_required
def admin_anuncios_novo():
    titulo = request.form.get('titulo', '').strip()
    posicao = request.form.get('posicao', 'topo').strip()
    foto_url = request.form.get('foto_url', '').strip()
    link = request.form.get('link', '').strip()
    ocasiao_id = request.form.get('ocasiao_id') or None
    tipo_impressao_id = request.form.get('tipo_impressao_id') or None
    cidade_slug = request.form.get('cidade_slug') or None
    apenas_funcionalidades = 'apenas_funcionalidades' in request.form
    data_inicio = request.form.get('data_inicio') or None
    data_fim = request.form.get('data_fim') or None
    ativo = 'ativo' in request.form

    if not titulo or not foto_url or not link:
        return jsonify({'erro': 'Preencha título, imagem e link.'}), 400

    try:
        execute("""
            INSERT INTO brindes_anuncios
                (titulo, posicao, foto_url, link, ocasiao_id, tipo_impressao_id,
                 cidade_slug, apenas_funcionalidades, data_inicio, data_fim, ativo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (titulo, posicao, foto_url, link, ocasiao_id, tipo_impressao_id,
              cidade_slug, apenas_funcionalidades, data_inicio, data_fim, ativo))
        return jsonify({'ok': True})
    except Exception:
        return jsonify({'erro': 'Erro ao salvar o anúncio.'}), 400


@app.route('/admin/anuncios/<int:item_id>/editar', methods=['GET', 'POST'])
@admin_required
def admin_anuncios_editar(item_id):
    anuncio = query_one("SELECT * FROM brindes_anuncios WHERE id = %s", (item_id,))
    if not anuncio:
        abort(404)

    # GET: o drawer do admin busca os dados atuais pra preencher o formulário.
    if request.method == 'GET':
        return jsonify(anuncio)

    # POST: salva as alterações.
    titulo = request.form.get('titulo', '').strip()
    posicao = request.form.get('posicao', 'topo').strip()
    foto_url = request.form.get('foto_url', '').strip()
    link = request.form.get('link', '').strip()
    ocasiao_id = request.form.get('ocasiao_id') or None
    tipo_impressao_id = request.form.get('tipo_impressao_id') or None
    cidade_slug = request.form.get('cidade_slug') or None
    apenas_funcionalidades = 'apenas_funcionalidades' in request.form
    data_inicio = request.form.get('data_inicio') or None
    data_fim = request.form.get('data_fim') or None
    ativo = 'ativo' in request.form

    if not titulo or not foto_url or not link:
        return jsonify({'erro': 'Preencha título, imagem e link.'}), 400

    try:
        execute("""
            UPDATE brindes_anuncios
            SET titulo = %s, posicao = %s, foto_url = %s, link = %s,
                ocasiao_id = %s, tipo_impressao_id = %s, cidade_slug = %s,
                apenas_funcionalidades = %s, data_inicio = %s, data_fim = %s, ativo = %s
            WHERE id = %s
        """, (titulo, posicao, foto_url, link, ocasiao_id, tipo_impressao_id,
              cidade_slug, apenas_funcionalidades, data_inicio, data_fim, ativo, item_id))
        return jsonify({'ok': True})
    except Exception:
        return jsonify({'erro': 'Erro ao salvar o anúncio.'}), 400


@app.route('/admin/anuncios/<int:item_id>/toggle', methods=['POST'])
@admin_required
def admin_anuncios_toggle(item_id):
    execute("UPDATE brindes_anuncios SET ativo = NOT ativo WHERE id = %s", (item_id,))
    return jsonify({'ok': True})


@app.route('/admin/anuncios/<int:item_id>/deletar', methods=['POST'])
@admin_required
def admin_anuncios_deletar(item_id):
    execute("DELETE FROM brindes_anuncios WHERE id = %s", (item_id,))
    return jsonify({'ok': True})

from xml.sax.saxutils import escape


@app.route('/sitemap.xml')
def sitemap():
    """Gera o sitemap.xml dinamicamente a partir do banco. Só inclui páginas
    públicas que realmente têm conteúdo indexável — não inclui login, painel,
    admin, demo, nem páginas de QR do tipo 'link' (essas só redirecionam pra
    fora e não têm nada pro Google indexar)."""

    def fmt_data(valor):
        if not valor:
            return None
        try:
            return valor.strftime('%Y-%m-%d')
        except AttributeError:
            return None

    urls = []

    # --- Páginas estáticas principais ---
    urls.append({'loc': f'{BASE_URL}/', 'changefreq': 'daily', 'priority': '1.0'})
    urls.append({'loc': f'{BASE_URL}/diretorio', 'changefreq': 'daily', 'priority': '0.8'})
    urls.append({'loc': f'{BASE_URL}/gerar-qr', 'changefreq': 'monthly', 'priority': '0.6'})
    urls.append({'loc': f'{BASE_URL}/acessar-painel', 'changefreq': 'monthly', 'priority': '0.3'})

    # --- Ocasiões ---
    ocasioes = query_all("SELECT slug FROM brindes_ocasioes WHERE ativo = TRUE")
    for o in ocasioes:
        urls.append({
            'loc': f"{BASE_URL}/ocasiao/{o['slug']}",
            'changefreq': 'weekly',
            'priority': '0.7',
        })

    # --- Tipos de impressão ---
    tipos = query_all("SELECT slug FROM brindes_tipos_impressao WHERE ativo = TRUE")
    for t in tipos:
        urls.append({
            'loc': f"{BASE_URL}/impressao/{t['slug']}",
            'changefreq': 'weekly',
            'priority': '0.7',
        })

    # --- Brindes (catálogo) ---
    brindes = query_all("SELECT slug, created_at FROM brindes_brindes WHERE ativo = TRUE")
    for b in brindes:
        urls.append({
            'loc': f"{BASE_URL}/brinde/{b['slug']}",
            'lastmod': fmt_data(b.get('created_at')),
            'changefreq': 'weekly',
            'priority': '0.6',
        })

    # --- Empresas (diretório) ---
    empresas = query_all("SELECT slug, created_at, cidade_slug FROM brindes_empresas WHERE ativo = TRUE")
    for e in empresas:
        urls.append({
            'loc': f"{BASE_URL}/empresa/{e['slug']}",
            'lastmod': fmt_data(e.get('created_at')),
            'changefreq': 'weekly',
            'priority': '0.6',
        })

    # --- Cidades (distintas, a partir das empresas cadastradas) ---
    cidades = query_all("""
        SELECT DISTINCT cidade_slug FROM brindes_empresas
        WHERE ativo = TRUE AND cidade_slug IS NOT NULL AND cidade_slug != ''
    """)
    for c in cidades:
        urls.append({
            'loc': f"{BASE_URL}/cidade/{c['cidade_slug']}",
            'changefreq': 'weekly',
            'priority': '0.5',
        })

    # --- Páginas de QR próprias (só as que têm conteúdo, não as de redirect) ---
    paginas = query_all("""
        SELECT slug, created_at FROM brindes_paginas
        WHERE ativo = TRUE AND tipo_destino = 'pagina'
    """)
    for p in paginas:
        urls.append({
            'loc': f"{BASE_URL}/{p['slug']}",
            'lastmod': fmt_data(p.get('created_at')),
            'changefreq': 'monthly',
            'priority': '0.4',
        })

    # --- Monta o XML ---
    partes = ['<?xml version="1.0" encoding="UTF-8"?>',
              '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for u in urls:
        partes.append('  <url>')
        partes.append(f"    <loc>{escape(u['loc'])}</loc>")
        if u.get('lastmod'):
            partes.append(f"    <lastmod>{u['lastmod']}</lastmod>")
        if u.get('changefreq'):
            partes.append(f"    <changefreq>{u['changefreq']}</changefreq>")
        if u.get('priority'):
            partes.append(f"    <priority>{u['priority']}</priority>")
        partes.append('  </url>')
    partes.append('</urlset>')

    xml = '\n'.join(partes)
    return app.response_class(xml, mimetype='application/xml')


@app.route('/robots.txt')
def robots_txt():
    """Aponta o Google pro sitemap. Se você já tiver um robots.txt estático
    em static/, pode remover essa rota — mas confirme que ele referencia
    o sitemap, senão o Google demora muito mais pra achar."""
    conteudo = f"User-agent: *\nAllow: /\nSitemap: {BASE_URL}/sitemap.xml\n"
    return app.response_class(conteudo, mimetype='text/plain')
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002, debug=True)
