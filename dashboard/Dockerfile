# Dashboard: Flask + the database driver. The code (dashboard/ and lb/, whose
# config module builds the connection string) is mounted read-only by
# docker-compose.yml, so editing it needs no rebuild. numpy is only there
# because importing lb.config imports the strategies package.
FROM python:3.11-slim
RUN pip install --no-cache-dir Flask==3.1.3 "psycopg[binary]==3.3.6" python-dotenv==1.2.4 numpy==2.4.6
