# Load balancer image: Python plus the LB's dependencies, pinned as in
# requirements.txt. scikit-learn must be the exact version the shipped model
# (ml/models/latency_model.joblib) was saved with: a pickled model is only
# guaranteed to load in the version that wrote it. The code is mounted
# read-only by docker-compose.yml, so editing it needs no rebuild.
FROM python:3.11-slim
RUN pip install --no-cache-dir "psycopg[binary]==3.3.6" python-dotenv==1.2.4 numpy==2.4.6 scikit-learn==1.9.1
