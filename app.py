from flask import Flask, render_template, request, redirect, session, flash, url_for, abort, send_file
import psycopg2
import psycopg2.extras
import requests
import os
import io
from datetime import datetime, timedelta
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash, check_password_hash
import qrcode

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "chave_secreta_qrcodebrindes")

# --- CONFIGURAÇÕES ---
BASE_URL = os.getenv("BASE_URL", "https://qrcodebrindes.com")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "213.199.56.207"),
    "port": int(os.getenv("DB_PORT", 5462)),
    "dbname": os.getenv("DB_NAME", "metro"),
    "user": os.getenv("DB_USER", "leanttro"),
    "password": os.getenv("DB_PASSWORD", "Fin@2021"),
}

IMGUR_CLIENT_ID = os.getenv("IMGUR_CLIENT_ID", "")

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
    return render_template('home.html')


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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002, debug=True)
