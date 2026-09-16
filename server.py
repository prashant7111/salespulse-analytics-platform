from pathlib import Path
from io import BytesIO
import json
import time
import threading
import sqlite3
import secrets
import urllib.request
import os
import ipaddress
import socket
from urllib.parse import urlparse
from functools import wraps

import pandas as pd
from flask import Flask, jsonify, render_template, request, session, redirect, send_file
from werkzeug.security import generate_password_hash, check_password_hash

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data' / 'train.csv'
UPLOADS = ROOT / 'data' / 'uploads'
UPLOADS.mkdir(parents=True, exist_ok=True)
DB = ROOT / 'data' / 'salespulse.db'

app = Flask(__name__)
SECRET_FILE = ROOT / 'data' / '.session_secret'
APP_SECRET = os.getenv('SALESPULSE_SECRET_KEY', '').strip()
if not APP_SECRET and SECRET_FILE.exists():
    APP_SECRET = SECRET_FILE.read_text(encoding='utf-8').strip()
if not APP_SECRET:
    APP_SECRET = secrets.token_urlsafe(48)
    if os.getenv('SALESPULSE_PERSIST_LOCAL_SECRET', '1') == '1':
        SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_text(APP_SECRET, encoding='utf-8')
app.secret_key = APP_SECRET
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    MAX_CONTENT_LENGTH=int(os.getenv('SALESPULSE_MAX_UPLOAD_BYTES', str(50 * 1024 * 1024))),
    SESSION_COOKIE_SECURE=os.getenv('SALESPULSE_COOKIE_SECURE', '0') == '1',
)
ALLOWED_EXT = {'.csv', '.xlsx', '.xls', '.json', '.jsonl', '.tsv', '.parquet'}
STATES = {}
LOCK = threading.RLock()


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute('''CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        created_at REAL NOT NULL
    )''')
    c.commit()
    return c


def seed():
    c = db()
    row = c.execute('SELECT id FROM users LIMIT 1').fetchone()
    if not row:
        c.execute(
            'INSERT INTO users(name,email,password,created_at) VALUES(?,?,?,?)',
            ('Demo Analyst', 'demo@salespulse.local', generate_password_hash('salespulse'), time.time())
        )
        c.commit()
    c.close()


seed()


def uid():
    return session.get('uid')


def login_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not uid():
            return jsonify(ok=False, message='Please sign in to continue.'), 401
        return fn(*args, **kwargs)
    return wrapped


def state_for(user_id=None):
    user_id = user_id or uid() or 0
    with LOCK:
        if user_id not in STATES:
            STATES[user_id] = {
                'source': 'Dataset · train.csv',
                'kind': 'dataset',
                'filename': 'train.csv',
                'live': False,
                'last_updated': time.time(),
                'df': load_dataset(),
                'error': None,
                'live_records': 0,
                'api_url': None,
                'api_token': None,
            }
        return STATES[user_id]


def clean_df(df):
    d = df.copy()
    d.columns = [str(c).strip() for c in d.columns]
    seen, cols = {}, []
    for c in d.columns:
        n = seen.get(c, 0)
        seen[c] = n + 1
        cols.append(c if n == 0 else f'{c}_{n}')
    d.columns = cols
    for c in d.columns:
        if d[c].dtype == 'object':
            d[c] = d[c].map(lambda x: x.strip() if isinstance(x, str) else x)
    return d


def read_any(source, filename):
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ValueError('Supported formats: CSV, TSV, Excel, JSON, JSONL, and Parquet.')
    raw = source.read() if hasattr(source, 'read') else Path(source).read_bytes()
    if not raw:
        raise ValueError('The selected file is empty.')
    try:
        if ext == '.csv':
            return pd.read_csv(BytesIO(raw))
        if ext == '.tsv':
            return pd.read_csv(BytesIO(raw), sep='\t')
        if ext in {'.xlsx', '.xls'}:
            return pd.read_excel(BytesIO(raw))
        if ext == '.parquet':
            return pd.read_parquet(BytesIO(raw))
        text = raw.decode('utf-8-sig')
        if ext == '.jsonl':
            return pd.DataFrame([json.loads(x) for x in text.splitlines() if x.strip()])
        payload = json.loads(text)
        if isinstance(payload, dict):
            for k in ('data', 'records', 'results', 'items'):
                if isinstance(payload.get(k), list):
                    payload = payload[k]
                    break
        if not isinstance(payload, list):
            raise ValueError('JSON must be an array of records or contain data, records, results, or items.')
        return pd.json_normalize(payload)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f'Could not parse the file: {exc}')


def norm_col(c):
    return ''.join(ch for ch in str(c).lower().strip() if ch.isalnum())


def numeric_ratio(series):
    if pd.api.types.is_numeric_dtype(series):
        return 1.0
    cleaned = series.astype(str).str.replace(r'[,₹$€£%]', '', regex=True).str.strip()
    return float(pd.to_numeric(cleaned, errors='coerce').notna().mean())


def is_identifier(c, series=None):
    n = norm_col(c)
    if any(n.endswith(w) for w in ('id', 'code', 'key', 'uuid', 'identifier')):
        return True
    if series is not None:
        try:
            unique = series.nunique(dropna=True)
            rows = max(len(series), 1)
            if unique / rows > .98 and not pd.api.types.is_numeric_dtype(series):
                return True
        except Exception:
            pass
    return False


def find_col(d, names):
    low = {str(c).lower().strip(): c for c in d.columns}
    normalized = {norm_col(c): c for c in d.columns}
    wanted = [norm_col(n) for n in names]
    for n in names:
        if n.lower().strip() in low:
            return low[n.lower().strip()]
    for n in wanted:
        if n in normalized:
            return normalized[n]
    for n in wanted:
        if len(n) < 5:
            continue
        for c in d.columns:
            nc = norm_col(c)
            if n in nc or nc in n:
                return c
    return None


def detect_numeric_columns(d):
    return [c for c in d.columns if not str(c).startswith('_') and numeric_ratio(d[c]) >= .80]


def first_numeric_metric(d):
    preferred = [
        'total sales value', 'total_sales_value', 'totalsalesvalue', 'sales value',
        'sales', 'revenue', 'net sales', 'amount', 'total amount', 'order value',
        'total value', 'turnover', 'income', 'profit', 'quantity', 'count', 'price', 'cost'
    ]
    numeric = detect_numeric_columns(d)
    for name in preferred:
        c = find_col(d, [name])
        if c in numeric and not is_identifier(c, d[c]):
            return c
    keywords = ('sales', 'revenue', 'amount', 'value', 'profit', 'income', 'turnover', 'quantity', 'count', 'price', 'cost', 'total', 'net')
    scored = []
    for c in numeric:
        if is_identifier(c, d[c]):
            continue
        n = norm_col(c)
        score = sum(2 for k in keywords if k in n) + numeric_ratio(d[c])
        scored.append((score, c))
    return sorted(scored, reverse=True)[0][1] if scored else (numeric[0] if numeric else None)


def coerce_numeric(series):
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors='coerce')
    return pd.to_numeric(series.astype(str).str.replace(r'[^0-9.\-]', '', regex=True), errors='coerce')


def enrich(d):
    d = clean_df(d)
    date = find_col(d, ['order date', 'order_date', 'date', 'datetime', 'timestamp', 'created_at', 'created at', 'time'])
    metric = first_numeric_metric(d)
    if date:
        d['_date'] = pd.to_datetime(d[date], dayfirst=True, errors='coerce')
    if metric:
        d['_metric'] = coerce_numeric(d[metric])
        d['_metric_source'] = str(metric)
    return d


def load_dataset():
    return enrich(pd.read_csv(DATA))


def current():
    return state_for()['df'].copy()


def money(v):
    v = float(v or 0)
    a = abs(v)
    if a >= 1e9:
        return f'{v / 1_000_000_000:.2f}B'
    if a >= 1e6:
        return f'{v / 1_000_000:.2f}M'
    if a >= 1e3:
        return f'{v / 1_000:.1f}K'
    return f'{v:,.0f}'


def fmt_count(v):
    return f'{int(v):,}'


def profile(d):
    rows, cols = d.shape
    miss = int(d.isna().sum().sum())
    cells = max(rows * cols, 1)
    numeric = detect_numeric_columns(d)
    dates = []
    for c in d.columns:
        if str(c).startswith('_'):
            continue
        if pd.api.types.is_datetime64_any_dtype(d[c]):
            dates.append(c)
            continue
        if d[c].dtype == 'object':
            sample = pd.to_datetime(d[c], errors='coerce', dayfirst=True)
            if sample.notna().mean() > .75:
                dates.append(c)
    categorical = [c for c in d.columns if c not in numeric and not str(c).startswith('_')]
    return {
        'rows': rows,
        'columns': cols,
        'numericColumns': len(numeric),
        'categoricalColumns': len(categorical),
        'missingValues': miss,
        'missingRate': round(miss / cells * 100, 2),
        'duplicateRows': int(d.duplicated().sum()),
        'dateColumns': dates,
        'columnsInfo': [
            {'name': str(c), 'type': str(d[c].dtype), 'nonNull': int(d[c].notna().sum()),
             'missing': int(d[c].isna().sum()), 'unique': int(d[c].nunique(dropna=True))}
            for c in d.columns if not str(c).startswith('_')
        ][:100]
    }


def dashboard(d):
    d = enrich(d)
    p = profile(d)
    metric = first_numeric_metric(d)
    metric_source = str(metric) if metric else None
    date = find_col(d, ['_date', 'order date', 'order_date', 'date', 'datetime', 'timestamp', 'created_at', 'created at', 'time'])
    order = find_col(d, ['order id', 'order_id', 'transaction id', 'transaction_id', 'invoice', 'invoice id'])
    customer = find_col(d, ['customer id', 'customer_id', 'customer', 'client'])
    product = find_col(d, ['product id', 'product_id', 'product', 'sku', 'item'])
    vals = coerce_numeric(d[metric]).fillna(0) if metric else pd.Series(0, index=d.index)
    k = {
        'records': len(d),
        'metricName': metric_source or 'Record Count',
        'metricTotal': float(vals.sum()) if metric else float(len(d)),
        'metricDisplay': money(vals.sum()) if metric else fmt_count(len(d)),
        'orders': int(d[order].nunique()) if order else len(d),
        'customers': int(d[customer].nunique()) if customer else None,
        'products': int(d[product].nunique()) if product else None,
        'hasNumericMetric': bool(metric),
        'measures': [str(c) for c in detect_numeric_columns(d) if not is_identifier(c, d[c])],
    }
    if date and d[date].notna().any():
        k['dateMin'] = d[date].min().strftime('%d %b %Y')
        k['dateMax'] = d[date].max().strftime('%d %b %Y')

    def groups(names, limit=12):
        c = find_col(d, names)
        if not c:
            return [], None
        labels = d[c].astype(str).replace('nan', 'Unknown')
        if metric:
            x = pd.DataFrame({'label': labels, 'value': vals}).groupby('label', as_index=False).agg(
                value=('value', 'sum'), records=('value', 'size')
            ).sort_values('value', ascending=False).head(limit)
        else:
            x = labels.value_counts().head(limit).rename_axis('label').reset_index(name='records')
            x['value'] = x['records']
        return x.to_dict('records'), str(c)

    monthly = []
    if date and metric and d[date].notna().any():
        x = pd.DataFrame({'date': d[date], 'v': vals}).dropna()
        x['period'] = x['date'].dt.to_period('M').astype(str)
        monthly = x.groupby('period', as_index=False).v.sum().rename(columns={'period': 'label', 'v': 'value'}).to_dict('records')

    cats, cat_col = groups(['category', 'product category', 'type'])
    regions, region_col = groups(['region', 'state', 'country', 'city'])
    segments, segment_col = groups(['segment', 'customer segment'])
    customers, customer_col = groups(['customer name', 'customer', 'customer id', 'client', 'salesperson', 'salesperson id'], 10)
    products, product_col = groups(['product name', 'product', 'product id', 'sku', 'item'], 12)

    # General-purpose dimensions for unfamiliar datasets.
    dims = []
    for c in d.columns:
        if str(c).startswith('_') or pd.api.types.is_numeric_dtype(d[c]):
            continue
        unique = d[c].nunique(dropna=True)
        if 2 <= unique <= min(20, max(2, int(len(d) * 0.15))):
            dims.append((unique, c))
    dims = [c for _, c in sorted(dims)]
    if not cats and dims:
        cats, cat_col = groups([dims[0]])
    if not segments and len(dims) > 1:
        segments, segment_col = groups([dims[1]])
    if not regions and len(dims) > 2:
        regions, region_col = groups([dims[2]])

    signals = []
    if p['missingRate'] > 5:
        signals.append(('Data quality', 'A noticeable share of cells is missing. Review incomplete fields before using the dashboard for operational decisions.'))
    if p['duplicateRows']:
        signals.append(('Duplicate records', f"{p['duplicateRows']:,} duplicate rows were detected. Confirm whether they are repeated events or accidental duplicates."))
    if not metric:
        signals.append(('No numeric measure', 'No suitable numeric measure was detected. The overview is using record count; value-based charts are not shown.'))
    if monthly and len(monthly) > 1:
        last, prev = monthly[-1]['value'], monthly[-2]['value']
        growth = (last - prev) / prev * 100 if prev else 0
        signals.append(('Latest period', f'The latest period changed by {growth:+.1f}% compared with the previous period.'))
    if not signals:
        signals.append(('Dataset ready', 'The dataset has been profiled and the available measures and dimensions have been mapped into the workspace.'))

    return {
        'profile': p, 'kpis': k, 'monthly': monthly,
        'category': cats, 'region': regions, 'segment': segments,
        'customers': customers, 'products': products,
        'fieldMap': {'category': cat_col, 'region': region_col, 'segment': segment_col, 'customer': customer_col, 'product': product_col, 'date': date},
        'signals': [{'title': a, 'text': b} for a, b in signals]
    }


CHARTS = {
    'trend': ('Trend over time', 'monthly'),
    'category': ('Category mix', 'category'),
    'region': ('Regional / geographic performance', 'region'),
    'segment': ('Segment mix', 'segment'),
    'customers': ('Top customers / entities', 'customers'),
    'products': ('Top products / items', 'products'),
}


@app.route('/')
def home():
    return redirect('/app') if uid() else render_template('login.html')


@app.route('/app')
@app.route('/app/<view>')
def app_page(view='overview'):
    if not uid():
        return redirect('/')
    allowed = {'overview', 'sources', 'intelligence', 'quality', 'explorer', 'settings', 'welcome'}
    if view == 'welcome':
        return render_template('welcome.html')
    return render_template('index.html', active_view=view if view in allowed else 'overview')


@app.route('/app/chart/<chart_name>')
def chart_page(chart_name):
    if not uid():
        return redirect('/')
    if chart_name not in CHARTS:
        return redirect('/app/intelligence')
    title, key = CHARTS[chart_name]
    return render_template('chart.html', chart_name=chart_name, chart_title=title, data_key=key)


@app.post('/api/auth/register')
def register():
    q = request.get_json() or {}
    name = str(q.get('name', '')).strip()
    email = str(q.get('email', '')).strip().lower()
    pw = str(q.get('password', ''))
    if len(name) < 2 or '@' not in email or '.' not in email.split('@')[-1] or len(pw) < 6:
        return jsonify(ok=False, message='Enter a valid name, email address, and a password of at least 6 characters.'), 400
    try:
        c = db()
        cur = c.execute(
            'INSERT INTO users(name,email,password,created_at) VALUES(?,?,?,?)',
            (name, email, generate_password_hash(pw), time.time())
        )
        c.commit()
        session['uid'] = cur.lastrowid
        session['name'] = name
        c.close()
        return jsonify(ok=True, redirect='/app/welcome')
    except sqlite3.IntegrityError:
        return jsonify(ok=False, message='An account with that email already exists. Sign in instead.'), 400


@app.post('/api/auth/login')
def login():
    q = request.get_json() or {}
    email = str(q.get('email', '')).strip().lower()
    pw = str(q.get('password', ''))
    c = db()
    u = c.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
    c.close()
    if not u or not check_password_hash(u['password'], pw):
        return jsonify(ok=False, message='Incorrect email or password.'), 401
    session['uid'] = u['id']
    session['name'] = u['name']
    return jsonify(ok=True, name=u['name'], redirect='/app/welcome')


@app.post('/api/auth/logout')
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get('/api/me')
def me():
    return jsonify(authenticated=bool(uid()), name=session.get('name', ''))


@app.post('/api/upload')
@login_required
def upload():
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify(ok=False, message='Choose a file first.'), 400
    try:
        d = enrich(read_any(f.stream, f.filename))
        if d.empty:
            raise ValueError('The uploaded file contains no records.')
        s = state_for()
        s.update(source=f.filename, kind='file', filename=f.filename, live=False, last_updated=time.time(), df=d,
                 error=None, live_records=len(d), api_url=None, api_token=None)
        return jsonify(ok=True, message=f'{f.filename} loaded successfully.', profile=profile(d), dashboard=dashboard(d))
    except Exception as exc:
        return jsonify(ok=False, message=f'Could not read this file safely: {exc}'), 400


@app.post('/api/source/remove')
@login_required
def remove_source():
    s = state_for()
    s['live'] = False
    s.update(source='No source connected', kind='empty', filename='', last_updated=time.time(),
             df=pd.DataFrame(), error=None, live_records=0, api_url=None, api_token=None)
    return jsonify(ok=True, message='Current source removed. Your workspace is now empty.')


@app.post('/api/source/reset')
@login_required
def reset_source():
    s = state_for()
    s['live'] = False
    s.update(source='Dataset · train.csv', kind='dataset', filename='train.csv', last_updated=time.time(),
             df=load_dataset(), error=None, live_records=0, api_url=None, api_token=None)
    return jsonify(ok=True, message='Your workspace is back on the built-in dataset.')


@app.post('/api/demo/start')
@login_required
def demo_start():
    s = state_for()
    base = load_dataset()
    s.update(source='Live dataset replay', kind='live-demo', filename='train.csv', live=True,
             last_updated=time.time(), df=base.iloc[:0].copy(), error=None, live_records=0)

    def worker():
        for i in range(0, len(base), 20):
            with LOCK:
                if not s['live']:
                    break
                s['df'] = pd.concat([s['df'], base.iloc[i:i + 20]], ignore_index=True)
                s['live_records'] = len(s['df'])
                s['last_updated'] = time.time()
            time.sleep(2)
        s['live'] = False

    threading.Thread(target=worker, daemon=True).start()
    return jsonify(ok=True, message='Live dataset replay started. New records will arrive in small batches.')


@app.post('/api/live/stop')
@login_required
def stop():
    state_for()['live'] = False
    return jsonify(ok=True, message='Live updates stopped.')


def validate_remote_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Use an HTTP(S) URL without embedded credentials.')
    host = parsed.hostname
    try:
        infos = socket.getaddrinfo(host, None)
        addresses = {item[4][0] for item in infos}
    except socket.gaierror as exc:
        raise ValueError(f'Could not resolve the API host: {exc}')
    blocked = []
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            blocked.append(address)
    if blocked:
        raise ValueError('Private, local, or reserved network targets are not allowed for live API ingestion.')


@app.post('/api/live/api/start')
@login_required
def api_start():
    q = request.get_json() or {}
    url = str(q.get('url', '')).strip()
    token = str(q.get('token', '')).strip()
    try:
        interval = max(5, min(int(q.get('interval', 15)), 300))
    except Exception:
        interval = 15
    if not url.startswith(('http://', 'https://')):
        return jsonify(ok=False, message='Enter a valid HTTP or HTTPS endpoint.'), 400
    try:
        validate_remote_url(url)
    except ValueError as exc:
        return jsonify(ok=False, message=str(exc)), 400
    s = state_for()
    s.update(source='Live API', kind='api', api_url=url, api_token=token or None, live=True, error=None, last_updated=time.time())

    def worker():
        while s['live']:
            try:
                req = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'SalesPulse/6.0'})
                if token:
                    req.add_header('Authorization', 'Bearer ' + token)
                with urllib.request.urlopen(req, timeout=10) as r:
                    payload = json.loads(r.read(8_000_000).decode('utf-8'))
                if isinstance(payload, dict):
                    for k in ('data', 'records', 'results', 'items'):
                        if isinstance(payload.get(k), list):
                            payload = payload[k]
                            break
                if not isinstance(payload, list):
                    raise ValueError('The API response must be a JSON array or contain data, records, results, or items.')
                incoming = enrich(pd.json_normalize(payload))
                s['df'] = incoming
                s['live_records'] = len(incoming)
                s['last_updated'] = time.time()
                s['error'] = None
            except Exception as exc:
                s['error'] = str(exc)
                s['last_updated'] = time.time()
            time.sleep(interval)

    threading.Thread(target=worker, daemon=True).start()
    return jsonify(ok=True, message='Live API monitoring started.')


@app.post('/api/webhook')
@login_required
def webhook():
    expected = os.getenv('SALESPULSE_WEBHOOK_TOKEN', '').strip()
    if expected and not secrets.compare_digest(request.headers.get('X-SalesPulse-Token', ''), expected):
        return jsonify(ok=False, message='Webhook authentication failed.'), 401
    q = request.get_json(silent=True)
    payload = q if isinstance(q, list) else (q.get('data', q.get('records', q.get('item'))) if isinstance(q, dict) else None)
    if payload is None and isinstance(q, dict):
        payload = [q]
    if not isinstance(payload, list):
        return jsonify(ok=False, message='Send a JSON object, array, or data/records payload.'), 400
    try:
        d = enrich(pd.json_normalize(payload))
        if d.empty:
            return jsonify(ok=False, message='The webhook payload did not contain any records.'), 400
        s = state_for()
        s['df'] = pd.concat([s['df'], d], ignore_index=True)
        s.update(live=True, kind='webhook', source='Webhook events', last_updated=time.time(), live_records=len(s['df']), error=None)
        return jsonify(ok=True, records=len(d))
    except Exception as exc:
        return jsonify(ok=False, message=f'Webhook payload could not be processed: {exc}'), 400


@app.get('/api/status')
@login_required
def status():
    s = state_for()
    return jsonify(source=s['source'], kind=s['kind'], filename=s['filename'], live=s['live'],
                   lastUpdated=s['last_updated'], records=len(s['df']), error=s['error'])


@app.get('/api/dashboard')
@login_required
def dash():
    d = current()
    return jsonify(empty=d.empty, message='No records available.' if d.empty else '', **({} if d.empty else dashboard(d)))


@app.get('/api/profile')
@login_required
def prof():
    return jsonify(profile(current()))


@app.get('/api/records')
@login_required
def records():
    d = current()
    cols = [c for c in d.columns if not str(c).startswith('_')][:40]
    out = d[cols].tail(300).copy()
    for c in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[c]):
            out[c] = out[c].dt.strftime('%Y-%m-%d %H:%M:%S')
    return jsonify(out.fillna('').to_dict('records'))


@app.get('/api/chart/<chart_name>')
@login_required
def chart_data(chart_name):
    if chart_name not in CHARTS:
        return jsonify(ok=False, message='Chart not found.'), 404
    d = current()
    if d.empty:
        return jsonify(ok=False, message='No records are available.'), 400
    data = dashboard(d)
    title, key = CHARTS[chart_name]
    values = data.get(key, [])
    metric = data['kpis']['metricName']
    total = sum(float(x.get('value', 0) or 0) for x in values)
    return jsonify(ok=True, title=title, key=key, values=values, metric=metric, total=total,
                   profile=data['profile'], fieldMap=data['fieldMap'])


@app.get('/api/health')
def health():
    return jsonify(ok=True, service='SalesPulse', status='healthy')


@app.get('/api/export')
@login_required
def export_data():
    d = current()
    path = UPLOADS / f'export_{uid()}_{int(time.time())}.csv'
    d[[c for c in d.columns if not str(c).startswith('_')]].to_csv(path, index=False)
    return send_file(path, as_attachment=True, download_name='salespulse-data-export.csv')


if __name__ == '__main__':
    app.run(host=os.getenv('SALESPULSE_HOST', '127.0.0.1'), port=int(os.getenv('PORT', '5050')), debug=False)
