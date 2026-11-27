FROM python:3.14-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ENV OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2
COPY indexed_reference.py backfill_history.py import_coordinates.py districts.py refresh.py automatic_metadata.py lifecycle_results.py lifecycle_inbox.py molit.py features.py fetch_macro.py macro_features.py ./
COPY apply_lifecycle_baseline.py delete_complexes.py ./
COPY db/user_accounts.sql db/trade_status.sql db/regions.sql db/metadata_refresh.sql db/trade_revisions.sql db/
COPY accounts.py account_api.py database.py complex_list.py main.py inference.py reference_band.py seoul_reference.py joint_model.py joint_model.joblib export_public_json.py check_public_json.py benchmark_web.py refresh_metadata.py ./
COPY seoul_audit_indexes.csv verify_southern.py evaluate_districts.py evaluate_seoul_holdout.py train_seoul.py seongdong_macro_audit.csv verify_service.py validate.py validate_api.py reference_prices.csv macro_raw.csv audit.csv.gz ./
COPY db/gwangjin/ db/gwangjin/
CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
