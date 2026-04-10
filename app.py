import os
import re
import psycopg2
from flask import Flask, render_template, request, redirect, url_for, session, flash
from dotenv import dotenv_values

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")

ALLOWED_TYPES = ["TEXT", "VARCHAR(255)", "INTEGER", "BIGINT", "BOOLEAN", "FLOAT", "DATE", "TIMESTAMP"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_id(name):
    """Valida que un nombre sea seguro como identificador SQL."""
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*$", name):
        raise ValueError(f"Nombre inválido: '{name}'. Solo letras, números y guiones bajos.")
    return name


def try_connect(host, port, dbname, user, password):
    try:
        conn = psycopg2.connect(
            host=host, port=int(port), dbname=dbname,
            user=user, password=password, connect_timeout=5,
        )
        return conn, None
    except psycopg2.OperationalError as e:
        return None, str(e)


def get_conn(dbname=None):
    """Abre conexión usando los parámetros guardados en sesión."""
    params = session.get("db_params")
    if not params:
        return None, "Sin sesión activa."
    return try_connect(
        params["host"], params["port"],
        dbname or params["dbname"],
        params["user"], params["password"],
    )


# ---------------------------------------------------------------------------
# Conexión
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/form")
def form():
    return render_template("form.html")


@app.route("/connect/form", methods=["POST"])
def connect_form():
    params = {
        "host":     request.form.get("host", "").strip(),
        "port":     request.form.get("port", "5432").strip(),
        "dbname":   request.form.get("dbname", "").strip(),
        "user":     request.form.get("user", "").strip(),
        "password": request.form.get("password", "").strip(),
    }
    conn, error = try_connect(**params)
    if error:
        return render_template("form.html", error=error, params=params)
    conn.close()
    session["db_params"] = params
    return redirect(url_for("dashboard"))


@app.route("/connect/env", methods=["POST"])
def connect_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.exists(env_path):
        return render_template("index.html", error="No se encontró el archivo .env.")

    env = dotenv_values(env_path)
    required = ["POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"]
    missing = [k for k in required if not env.get(k)]
    if missing:
        return render_template("index.html", error=f"Faltan variables en .env: {', '.join(missing)}")

    params = {
        "host":     env["POSTGRES_HOST"],
        "port":     env["POSTGRES_PORT"],
        "dbname":   env["POSTGRES_DB"],
        "user":     env["POSTGRES_USER"],
        "password": env["POSTGRES_PASSWORD"],
    }
    conn, error = try_connect(**params)
    if error:
        return render_template("index.html", error=error)
    conn.close()
    session["db_params"] = params
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))


# ---------------------------------------------------------------------------
# Dashboard — lista de bases de datos
# ---------------------------------------------------------------------------

@app.route("/dashboard")
def dashboard():
    conn, error = get_conn()
    if error:
        return redirect(url_for("index"))

    with conn.cursor() as cur:
        cur.execute(
            "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname;"
        )
        databases = [row[0] for row in cur.fetchall()]
    conn.close()

    p = session["db_params"]
    return render_template(
        "dashboard.html",
        databases=databases,
        host=p["host"], port=p["port"], user=p["user"],
    )


@app.route("/dashboard/create-db", methods=["POST"])
def create_db():
    dbname = request.form.get("dbname", "").strip()
    try:
        sanitize_id(dbname)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("dashboard"))

    conn, error = get_conn()
    if error:
        return redirect(url_for("index"))

    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{dbname}"')
        flash(f'Base de datos "{dbname}" creada.', "ok")
    except psycopg2.Error as e:
        flash(str(e).strip(), "error")
    finally:
        conn.close()

    return redirect(url_for("database", dbname=dbname))


# ---------------------------------------------------------------------------
# Vista de base de datos — tablas
# ---------------------------------------------------------------------------

@app.route("/db/<dbname>")
def database(dbname):
    conn, error = get_conn(dbname)
    if error:
        flash(error, "error")
        return redirect(url_for("dashboard"))

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = 'public' ORDER BY table_name;
            """
        )
        tables = [row[0] for row in cur.fetchall()]
    conn.close()

    return render_template(
        "database.html",
        dbname=dbname,
        tables=tables,
        allowed_types=ALLOWED_TYPES,
    )


@app.route("/db/<dbname>/create-table", methods=["POST"])
def create_table(dbname):
    table_name = request.form.get("table_name", "").strip()
    col_names = request.form.getlist("col_name")
    col_types = request.form.getlist("col_type")

    try:
        sanitize_id(table_name)
        col_defs = []
        for name, typ in zip(col_names, col_types):
            name = name.strip()
            if not name:
                continue
            sanitize_id(name)
            if typ not in ALLOWED_TYPES:
                raise ValueError(f"Tipo no permitido: {typ}")
            col_defs.append(f'"{name}" {typ}')
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("database", dbname=dbname))

    if not col_defs:
        flash("Añade al menos una columna.", "error")
        return redirect(url_for("database", dbname=dbname))

    conn, error = get_conn(dbname)
    if error:
        flash(error, "error")
        return redirect(url_for("dashboard"))

    try:
        cols_sql = "id SERIAL PRIMARY KEY, " + ", ".join(col_defs)
        with conn.cursor() as cur:
            cur.execute(f'CREATE TABLE IF NOT EXISTS "{table_name}" ({cols_sql})')
        conn.commit()
        flash(f'Tabla "{table_name}" creada.', "ok")
    except psycopg2.Error as e:
        conn.rollback()
        flash(str(e).strip(), "error")
    finally:
        conn.close()

    return redirect(url_for("database", dbname=dbname))


# ---------------------------------------------------------------------------
# Vista de tabla — registros
# ---------------------------------------------------------------------------

@app.route("/db/<dbname>/<table>")
def table_view(dbname, table):
    conn, error = get_conn(dbname)
    if error:
        flash(error, "error")
        return redirect(url_for("dashboard"))

    try:
        sanitize_id(table)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("database", dbname=dbname))

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
              AND column_name != 'id'
            ORDER BY ordinal_position;
            """,
            (table,),
        )
        columns = cur.fetchall()  # [(name, type), ...]

        cur.execute(f'SELECT * FROM "{table}" ORDER BY id DESC LIMIT 200')
        rows = cur.fetchall()
        col_headers = [desc[0] for desc in cur.description]

    conn.close()
    return render_template(
        "table.html",
        dbname=dbname,
        table=table,
        columns=columns,
        rows=rows,
        col_headers=col_headers,
    )


@app.route("/db/<dbname>/<table>/insert", methods=["POST"])
def insert_record(dbname, table):
    try:
        sanitize_id(table)
    except ValueError as e:
        flash(str(e), "error")
        return redirect(url_for("database", dbname=dbname))

    conn, error = get_conn(dbname)
    if error:
        flash(error, "error")
        return redirect(url_for("dashboard"))

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
              AND column_name != 'id'
            ORDER BY ordinal_position;
            """,
            (table,),
        )
        col_names = [row[0] for row in cur.fetchall()]

    values = [request.form.get(col, "") or None for col in col_names]
    placeholders = ", ".join(["%s"] * len(col_names))
    cols_sql = ", ".join([f'"{c}"' for c in col_names])

    try:
        with conn.cursor() as cur:
            cur.execute(
                f'INSERT INTO "{table}" ({cols_sql}) VALUES ({placeholders})',
                values,
            )
        conn.commit()
        flash("Registro añadido.", "ok")
    except psycopg2.Error as e:
        conn.rollback()
        flash(str(e).strip(), "error")
    finally:
        conn.close()

    return redirect(url_for("table_view", dbname=dbname, table=table))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
