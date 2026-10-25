# Changelog
Convention: every behavior change bumps the version in the `model_handoff.py` header.

## 0.6.0
- Hide the old chat's sidebar entry after a successful delete, with retries over ~10s.
- Check the DELETE status; only hide on 200/404, log otherwise.
## 0.5.0
- Model card popup (icon, name, description) with fallback to plain popup (`show_model_card`).
- Fernet encryption of API keys at rest (`encrypt_api_keys`).
## 0.4.0
- Delete the origin chat after handoff when it held a single user message (`delete_old_chat`).
