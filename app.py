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

# --- CONFIGURAÇÕES ---
BASE_URL = os.getenv("BASE_URL", "https://qrcodebrindes.com")

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

# Link fixo de pagamento pra templates pagos (Parte 6 — defina o valor real depois).
LINK_PAGAMENTO_TEMPLATES = os.getenv("LINK_PAGAMENTO_TEMPLATES", "")

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


def carregar_templates():
    """Varre templates/paginas/ e devolve os metadados (<slug>.json) de cada
    template disponível (<slug>.html + <slug>.json lado a lado)."""
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
    return templates


def get_template_por_slug(slug):
    return next((t for t in carregar_templates() if t['slug'] == slug), None)


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


def gerar_timeline_events(pagina):
    """Placeholder: timeline vem de timeline_json (lista de {date, title})."""
    return pagina.get("timeline_json") or []


# --- ANÚNCIOS (mesmo padrão do hub, adaptado: sem hub_id, sem cidade/bairro) ---
def get_anuncio(posicao, ocasiao_id=None):
    """Retorna 1 anúncio ativo pra posição informada ('topo' ou 'meio'), dentro
    da vigência (data_inicio/data_fim). Se ocasiao_id for passado, prioriza um
    anúncio segmentado pra essa ocasião, mas também aceita um genérico
    (ocasiao_id NULL no banco = vale pra qualquer ocasião). Sem ocasiao_id
    (ex.: home, diretório), só considera anúncios genéricos."""
    hoje = datetime.now().date()

    if ocasiao_id:
        sql = """
            SELECT a.*, o.nome as ocasiao_nome
            FROM brindes_anuncios a
            LEFT JOIN brindes_ocasioes o ON o.id = a.ocasiao_id
            WHERE a.ativo = TRUE AND a.posicao = %s
              AND (a.data_inicio IS NULL OR a.data_inicio <= %s)
              AND (a.data_fim IS NULL OR a.data_fim >= %s)
              AND (a.ocasiao_id IS NULL OR a.ocasiao_id = %s)
            ORDER BY (a.ocasiao_id IS NOT NULL) DESC, RANDOM()
            LIMIT 1
        """
        return query_one(sql, (posicao, hoje, hoje, ocasiao_id))

    sql = """
        SELECT a.*, o.nome as ocasiao_nome
        FROM brindes_anuncios a
        LEFT JOIN brindes_ocasioes o ON o.id = a.ocasiao_id
        WHERE a.ativo = TRUE AND a.posicao = %s
          AND (a.data_inicio IS NULL OR a.data_inicio <= %s)
          AND (a.data_fim IS NULL OR a.data_fim >= %s)
          AND a.ocasiao_id IS NULL
        ORDER BY RANDOM()
        LIMIT 1
    """
    return query_one(sql, (posicao, hoje, hoje))


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


# --- GERAR QR CODE (criação de página) ---
@app.route('/gerar-qr', methods=['GET', 'POST'])
def gerar_qr():
    if not check_limit(f"gerar_{get_ip()}", 10, 3600):
        flash("Muitas tentativas. Tente novamente mais tarde.", "error")
        return redirect('/')

    if request.method == 'POST':
        slug = request.form.get('slug', '').lower().strip()
        senha = request.form.get('senha', '')
        tipo_destino = request.form.get('tipo_destino', 'link')
        destino_url = request.form.get('destino_url', '').strip()
        email = request.form.get('email', '').strip()
        titulo = request.form.get('titulo', '').strip()
        template_slug = request.form.get('template', 'classic')

        if not slug or not senha:
            flash('Preencha o link (slug) e a senha.', 'error')
            return render_template('gerar_qr.html', templates=carregar_templates(), current_year=datetime.now().year)

        if get_pagina_by_slug(slug):
            flash('Esse link já está em uso, escolha outro.', 'error')
            return render_template('gerar_qr.html', templates=carregar_templates(), current_year=datetime.now().year)

        if tipo_destino == 'pagina' and not email:
            flash('Email é obrigatório para criar uma página própria (usado para pagamento e recuperação).', 'error')
            return render_template('gerar_qr.html', templates=carregar_templates(), current_year=datetime.now().year)

        if tipo_destino == 'link' and not destino_url:
            flash('Cole o link de destino.', 'error')
            return render_template('gerar_qr.html', templates=carregar_templates(), current_year=datetime.now().year)

        if tipo_destino == 'pagina':
            tpl_escolhido = get_template_por_slug(template_slug)
            if tpl_escolhido and tpl_escolhido.get('tier') == 'pago':
                # Template pago: não cria a Pagina agora — explica o pagamento
                # e o pessoal libera manualmente depois de confirmar.
                return render_template(
                    'pagamento_pendente.html',
                    template=tpl_escolhido,
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

        flash('QR code criado com sucesso!', 'success')
        return redirect(f'/{slug}/painel')

    return render_template('gerar_qr.html', templates=carregar_templates(), current_year=datetime.now().year)


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
    return render_template(
        tpl['arquivo'],
        page=pagina,
        timeline_events=gerar_timeline_events(pagina),
        current_year=datetime.now().year,
        font_css="'Inter', sans-serif",
        font_size_val="1.1rem",
    )


# --- DEMO DE TEMPLATE (preview ao vivo, sem gravar nada no banco) ---
@app.route('/demo/<template_slug>')
def demo_template(template_slug):
    tpl = get_template_por_slug(template_slug)
    if not tpl:
        abort(404)
    pagina_fake = {**tpl.get('demo', {}), 'template': template_slug}
    return render_template(
        tpl['arquivo'], page=pagina_fake,
        timeline_events=pagina_fake.get('timeline', []),
        current_year=datetime.now().year,
        font_css="'Inter', sans-serif", font_size_val="1.1rem",
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


# --- LOGIN DA PÁGINA (slug + senha) ---
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
    session.clear()
    return redirect(f'/{slug}')


# --- PAINEL DA PÁGINA (edição) ---
@app.route('/<slug>/painel', methods=['GET', 'POST'])
def painel_pagina(slug):
    pagina = get_pagina_by_slug(slug)
    if not pagina:
        abort(404)

    if session.get('pagina_id') != pagina['id']:
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

        execute("""
            UPDATE brindes_paginas
            SET titulo = %s,
                mensagem = %s,
                foto_url = %s,
                template = %s,
                destino_url = %s,
                tipo_destino = %s,
                timeline_json = %s
            WHERE id = %s
        """, (
            request.form.get('titulo', ''),
            request.form.get('mensagem', ''),
            foto_url,
            template_slug,
            request.form.get('destino_url') or None,
            request.form.get('tipo_destino', pagina.get('tipo_destino')),
            timeline_json,
            pagina['id'],
        ))

        flash('Página atualizada com sucesso!', 'success')
        return redirect(f'/{slug}/painel')

    total_scans = query_one("SELECT COUNT(*) as total FROM brindes_scans WHERE pagina_id = %s", (pagina['id'],))
    pagina_atualizada = get_pagina_by_slug(slug)
    campos_por_template = {t['slug']: t.get('campos', []) for t in templates_disponiveis}

    return render_template(
        'painel.html',
        pagina=pagina_atualizada,
        total_scans=total_scans['total'] if total_scans else 0,
        qr_url=f"/{slug}/qr.png",
        templates=templates_disponiveis,
        campos_por_template=campos_por_template,
    )


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
    return render_template('negocio_brindes.html', empresa=empresa,
                            anuncio_topo=get_anuncio('topo'),
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
                            anuncio_topo=get_anuncio('topo'),
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
                            empresas=empresas, anuncio_topo=get_anuncio('topo'),
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
        'total_empresas': query_one("SELECT COUNT(*) as c FROM brindes_empresas")['c'],
        'leads_pendentes': query_one("SELECT COUNT(*) as c FROM brindes_leads WHERE status = 'pendente'")['c'],
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
    empresas = query_all("SELECT * FROM brindes_empresas ORDER BY nome")
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

    return dict(
        stats=stats, ocasioes=ocasioes, tipos=tipos,
        brindes=brindes, empresas=empresas, leads=leads, paginas=paginas,
        templates=templates,
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
        return jsonify({'ok': True})
    except Exception:
        return jsonify({'erro': 'Erro ao salvar o template.'}), 400


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
        SELECT a.*, o.nome as ocasiao_nome
        FROM brindes_anuncios a
        LEFT JOIN brindes_ocasioes o ON o.id = a.ocasiao_id
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
    data_inicio = request.form.get('data_inicio') or None
    data_fim = request.form.get('data_fim') or None
    ativo = 'ativo' in request.form

    if not titulo or not foto_url or not link:
        return jsonify({'erro': 'Preencha título, imagem e link.'}), 400

    try:
        execute("""
            INSERT INTO brindes_anuncios
                (titulo, posicao, foto_url, link, ocasiao_id, data_inicio, data_fim, ativo)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (titulo, posicao, foto_url, link, ocasiao_id, data_inicio, data_fim, ativo))
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
    data_inicio = request.form.get('data_inicio') or None
    data_fim = request.form.get('data_fim') or None
    ativo = 'ativo' in request.form

    if not titulo or not foto_url or not link:
        return jsonify({'erro': 'Preencha título, imagem e link.'}), 400

    try:
        execute("""
            UPDATE brindes_anuncios
            SET titulo = %s, posicao = %s, foto_url = %s, link = %s,
                ocasiao_id = %s, data_inicio = %s, data_fim = %s, ativo = %s
            WHERE id = %s
        """, (titulo, posicao, foto_url, link, ocasiao_id, data_inicio, data_fim, ativo, item_id))
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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002, debug=True)
