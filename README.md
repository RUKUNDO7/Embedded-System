# Rk Project Structure

This project now runs from the repository root (`R:\Embedded`).

```text
R:\Embedded
  app.py
  dashboard.html
  rk.db
  main.py
  README.md
  .gitignore
```

## Run the Dashboard

From `R:\Embedded`:

```bash
python app.py
```

Then open:

- `http://127.0.0.1:9215/`
- `http://157.173.101.159:9215/` (deployed dashboard)

## Files to Edit

- Backend/API: `app.py`
- Frontend/UI: `dashboard.html`
- RFID device script: `main.py`
