# Running PATREOAIX on PythonAnywhere (patreoaix.com)

On the laptop, `runserver` is one process that does everything. On PythonAnywhere the website runs
as several worker processes, has no websockets, and background jobs run as a separate task. The code
now handles all of that; these are the steps on the PythonAnywhere side.

## 1. Get the code

Bash console:

```bash
cd ~
git clone https://github.com/dicks6n/patreoaix.git      # first time
# later updates:  cd ~/patreoaix && git pull
```

## 2. Virtualenv and packages

```bash
mkvirtualenv patreoaix --python=python3.11              # once
workon patreoaix
cd ~/patreoaix
pip install -r requirements.txt
```

`torch` / `transformers` were moved to `requirements-ml.txt` (several hundred MB, not used by the site).

## 3. The `.env` file

```bash
cd ~/patreoaix
cp .env.example .env
nano .env
```

Server values: `DEBUG=False`, a new `SECRET_KEY`, `SITE_DOMAIN=patreoaix.com`, and your keys.
Use NEW keys (see "Security" below), not the ones that were in settings.py.

## 4. Database and static files

```bash
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py check_feeds            # which market-data sources this server can reach
```

## 5. Web tab

* **Source code:** `/home/YOURUSER/patreoaix`
* **Virtualenv:** `/home/YOURUSER/.virtualenvs/patreoaix`
* **WSGI configuration file** — replace its contents with:

```python
import os, sys
path = '/home/YOURUSER/patreoaix'
if path not in sys.path:
    sys.path.insert(0, path)
os.environ['DJANGO_SETTINGS_MODULE'] = 'project.settings'
from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
```

* **Static files** (two rows):

| URL       | Directory                                |
|-----------|------------------------------------------|
| `/static/` | `/home/YOURUSER/patreoaix/staticfiles`   |
| `/media/`  | `/home/YOURUSER/patreoaix/media`         |

* **Security:** enable *Force HTTPS* (after the certificate for patreoaix.com is active).
* Press **Reload**.

## 6. Background task (Tasks tab → Always-on tasks)

One task does what the extra terminals did on the laptop (order engine + AI signal scanner):

```
/home/YOURUSER/.virtualenvs/patreoaix/bin/python /home/YOURUSER/patreoaix/manage.py run_background
```

Without it, pending orders / S/L / T/P only run while someone has the terminal open, and the AI
scanner runs inside page requests (slow).

## 7. After every update

```bash
cd ~/patreoaix && git pull && workon patreoaix
pip install -r requirements.txt        # only if requirements changed
python manage.py migrate
python manage.py collectstatic --noinput
```

Then **Reload** on the Web tab and restart the always-on task.

## What changed so it behaves like the laptop

| Laptop (runserver)                    | PythonAnywhere                         | Fix in the code                                    |
|---------------------------------------|----------------------------------------|----------------------------------------------------|
| one process, in-memory cache          | many processes                         | `CACHES` = file cache shared by all processes      |
| websockets via daphne                 | no websockets (`/ws/` → 404)           | pages detect it and poll the REST API instead      |
| Binance / Yahoo reachable             | Binance 451, Yahoo 429                 | Deriv / Kraken / Coinbase first, failed sources paused |
| `websocket-client` installed          | maybe missing                          | built-in websocket client as fallback              |
| http://localhost                      | https://patreoaix.com                  | `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS`, secure cookies |
| DEBUG serves /static and /media       | DEBUG=False                            | `STATIC_ROOT` + static file mappings above         |
| workers in extra terminals            | always-on task                         | `manage.py run_background`                         |

## Security

The GitHub repository is public and the old settings.py contained live secrets. They are removed from
the code now, but they stay in the git history, so treat them as leaked and replace them:

* NOWPayments: new API key and IPN secret
* Gmail: delete the app password, create a new one
* TwelveData: new API key
* M-Pesa (Daraja): new consumer key / secret
* Django: new `SECRET_KEY`

Put the new values only in `.env` (on the laptop and on the server).
Or make the repository private on GitHub.
