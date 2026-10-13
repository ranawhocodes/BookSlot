import os
from app import create_app

env_name = os.environ.get("FLASK_ENV", "dev")
app = create_app(env_name)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
