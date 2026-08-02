# Backend image: FastAPI on uvicorn.
#
# Kept deliberately boring. psycopg[binary] and bcrypt both ship prebuilt
# wheels, so there is no C toolchain in here and the image stays small.
#
# Test dependencies are installed alongside the runtime ones. That is a
# deliberate choice for a hackathon: CI runs pytest *inside this image*, so
# what the tests pass against is byte-for-byte what gets deployed.

FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Requirements are copied on their own first. Docker caches each step, so as
# long as this file is unchanged a rebuild skips the whole install — editing
# application code does not re-download a single package.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# Run as a normal user. If the app is ever compromised, the attacker is not
# root inside the container.
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

EXPOSE 8000

# One worker on purpose (ARCHITECTURE.md §14.4): each extra worker is another
# full copy of the interpreter, and the box has 2 GB.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
