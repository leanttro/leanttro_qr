from flask import Flask, render_template, request, redirect, session, flash, url_for, abort, send_file
import psycopg2
import psycopg2.extras
import requests
import os
import io
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

TEMPLATES_DISPONIVEIS = {
    "classic": "love/index.html",
    "stitch": "love/stitch.html",
}

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


# --- ROTA RAIZ ---
@app.route('/')
def index():
    return render_template('home.html', current_year=datetime.now().year)


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

        if not slug or not senha:
            flash('Preencha o link (slug) e a senha.', 'error')
            return render_template('gerar_qr.html')

        if get_pagina_by_slug(slug):
            flash('Esse link já está em uso, escolha outro.', 'error')
            return render_template('gerar_qr.html')

        if tipo_destino == 'pagina' and not email:
            flash('Email é obrigatório para criar uma página própria (usado para pagamento e recuperação).', 'error')
            return render_template('gerar_qr.html')

        if tipo_destino == 'link' and not destino_url:
            flash('Cole o link de destino.', 'error')
            return render_template('gerar_qr.html')

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
            request.form.get('template', 'classic'),
            titulo,
            email or None,
        ))

        flash('QR code criado com sucesso!', 'success')
        return redirect(f'/{slug}/painel')

    return render_template('gerar_qr.html')


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
    if slug in ['static', 'favicon.ico', 'gerar-qr', 'diretorio', 'ocasiao', 'brinde']:
        abort(404)

    pagina = get_pagina_by_slug(slug)
    if not pagina:
        abort(404)

    registrar_scan(pagina['id'])

    if pagina['tipo_destino'] == 'link' and pagina['destino_url']:
        return redirect(pagina['destino_url'])

    return render_pagina(pagina)


def render_pagina(pagina):
    template_path = TEMPLATES_DISPONIVEIS.get(pagina.get('template'), TEMPLATES_DISPONIVEIS['classic'])
    return render_template(
        template_path,
        page=pagina,
        timeline_events=gerar_timeline_events(pagina),
        current_year=datetime.now().year,
        font_css="'Inter', sans-serif",
        font_size_val="1.1rem",
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

    if request.method == 'POST':
        foto_url = pagina.get('foto_url')
        f = request.files.get('foto')
        if f and f.filename:
            nova_url = upload_imgur(f)
            if nova_url:
                foto_url = nova_url
            else:
                flash('Dados salvos, mas houve erro ao enviar a foto.', 'error')

        execute("""
            UPDATE brindes_paginas
            SET titulo = %s,
                mensagem = %s,
                foto_url = %s,
                template = %s,
                destino_url = %s,
                tipo_destino = %s
            WHERE id = %s
        """, (
            request.form.get('titulo', ''),
            request.form.get('mensagem', ''),
            foto_url,
            request.form.get('template', pagina.get('template', 'classic')),
            request.form.get('destino_url') or None,
            request.form.get('tipo_destino', pagina.get('tipo_destino')),
            pagina['id'],
        ))

        flash('Página atualizada com sucesso!', 'success')
        return redirect(f'/{slug}/painel')

    total_scans = query_one("SELECT COUNT(*) as total FROM brindes_scans WHERE pagina_id = %s", (pagina['id'],))
    pagina_atualizada = get_pagina_by_slug(slug)

    return render_template(
        'painel.html',
        pagina=pagina_atualizada,
        total_scans=total_scans['total'] if total_scans else 0,
        qr_url=f"/{slug}/qr.png",
    )


# =====================================================================
#  CATÁLOGO E DIRETÓRIO — páginas públicas
# =====================================================================

@app.route('/ocasiao/<slug>')
def ocasiao_publica(slug):
    ocasiao = query_one("SELECT * FROM brindes_ocasioes WHERE slug = %s AND ativo = TRUE", (slug,))
    if not ocasiao:
        abort(404)

    brindes = query_all("""
        SELECT b.*, t.nome as tipo_nome
        FROM brindes_brindes b
        LEFT JOIN brindes_tipos_impressao t ON t.id = b.tipo_impressao_id
        WHERE b.ocasiao_id = %s AND b.ativo = TRUE
        ORDER BY b.nome
    """, (ocasiao['id'],))

    return render_template('ocasiao.html', ocasiao=ocasiao, brindes=brindes)


@app.route('/brinde/<slug>')
def brinde_publico(slug):
    brinde = query_one("""
        SELECT b.*, o.nome as ocasiao_nome, o.slug as ocasiao_slug, t.nome as tipo_nome
        FROM brindes_brindes b
        LEFT JOIN brindes_ocasioes o ON o.id = b.ocasiao_id
        LEFT JOIN brindes_tipos_impressao t ON t.id = b.tipo_impressao_id
        WHERE b.slug = %s AND b.ativo = TRUE
    """, (slug,))
    if not brinde:
        abort(404)

    return render_template('brinde.html', brinde=brinde)


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
    return render_template('diretorio.html', empresas=empresas)


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


@app.route('/admin')
@admin_required
def admin_dashboard():
    stats = {
        'total_paginas': query_one("SELECT COUNT(*) as c FROM brindes_paginas")['c'],
        'total_scans': query_one("SELECT COUNT(*) as c FROM brindes_scans")['c'],
        'total_brindes': query_one("SELECT COUNT(*) as c FROM brindes_brindes")['c'],
        'total_empresas': query_one("SELECT COUNT(*) as c FROM brindes_empresas")['c'],
        'leads_pendentes': query_one("SELECT COUNT(*) as c FROM brindes_leads WHERE status = 'pendente'")['c'],
    }
    ocasioes = query_all("SELECT * FROM brindes_ocasioes ORDER BY nome")
    tipos = query_all("SELECT * FROM brindes_tipos_impressao ORDER BY nome")
    brindes = query_all("""
        SELECT b.*, o.nome as ocasiao_nome, t.nome as tipo_nome
        FROM brindes_brindes b
        LEFT JOIN brindes_ocasioes o ON o.id = b.ocasiao_id
        LEFT JOIN brindes_tipos_impressao t ON t.id = b.tipo_impressao_id
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

    return render_template(
        'admin.html',
        stats=stats, ocasioes=ocasioes, tipos=tipos,
        brindes=brindes, empresas=empresas, leads=leads,
    )


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
    if nome and slug:
        try:
            execute("INSERT INTO brindes_tipos_impressao (nome, slug) VALUES (%s, %s)", (nome, slug))
            flash('Tipo de impressão adicionado.', 'success')
        except Exception:
            flash('Erro ao adicionar (slug já existe?).', 'error')
    return redirect('/admin#tipos')


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
    tipo_impressao_id = request.form.get('tipo_impressao_id') or None

    imagem_url = None
    f = request.files.get('imagem')
    if f and f.filename:
        imagem_url = upload_imgur(f)

    if nome and slug:
        try:
            execute("""
                INSERT INTO brindes_brindes (nome, slug, descricao, ocasiao_id, tipo_impressao_id, imagem_url)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (nome, slug, descricao, ocasiao_id, tipo_impressao_id, imagem_url))
            flash('Brinde adicionado.', 'success')
        except Exception:
            flash('Erro ao adicionar (slug já existe?).', 'error')
    return redirect('/admin#brindes')


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
    whatsapp = request.form.get('whatsapp', '').strip()
    cidade = request.form.get('cidade', '').strip()
    plano = request.form.get('plano', 'gratis')

    if nome and slug:
        try:
            execute("""
                INSERT INTO brindes_empresas (nome, slug, email, whatsapp, cidade, plano)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (nome, slug, email or None, whatsapp or None, cidade or None, plano))
            flash('Empresa adicionada.', 'success')
        except Exception:
            flash('Erro ao adicionar (slug já existe?).', 'error')
    return redirect('/admin#empresas')


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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002, debug=True)
