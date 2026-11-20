FROM python:3.14-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ENV OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2
COPY districts.py refresh.py molit.py features.py fetch_macro.py macro_features.py ./
COPY db/user_accounts.sql db/trade_status.sql db/regions.sql db/
COPY accounts.py account_api.py database.py main.py inference.py joint_model.py joint_model.joblib ./
COPY evaluate_districts.py seongdong_macro_audit.csv verify_service.py validate.py validate_api.py reference_prices.csv macro_raw.csv audit.csv.gz ./
COPY db/gwangjin/ db/gwangjin/
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]



