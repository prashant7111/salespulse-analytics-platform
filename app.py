"""SalesPulse local one-click launcher.

For hosted deployments use wsgi.py + gunicorn instead of this launcher.
"""
from pathlib import Path
import importlib.util, subprocess, sys, threading, time, webbrowser, os

ROOT = Path(__file__).resolve().parent
REQ = ROOT / 'requirements.txt'

def bootstrap():
    packages=[('flask','Flask'),('pandas','pandas'),('openpyxl','openpyxl'),('xlrd','xlrd'),('pyarrow','pyarrow')]
    missing=[pip for mod,pip in packages if importlib.util.find_spec(mod) is None]
    if not missing:
        return
    print('SalesPulse: installing missing packages:', ', '.join(missing))
    try:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '-r', str(REQ)])
    except subprocess.CalledProcessError:
        subprocess.check_call([sys.executable, '-m', 'pip', 'install', '--user', '-r', str(REQ)])

bootstrap()
from server import app

if __name__ == '__main__':
    host = os.getenv('SALESPULSE_HOST', '127.0.0.1')
    port = int(os.getenv('PORT', '5050'))
    if os.getenv('SALESPULSE_AUTO_OPEN', '1') == '1':
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(f'http://{host}:{port}')), daemon=True).start()
    print(f'\nSalesPulse is running at http://{host}:{port}\n')
    app.run(host=host, port=port, debug=False, use_reloader=False)
