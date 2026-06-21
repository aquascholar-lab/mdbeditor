import os
import re
import glob
import shutil
import tempfile
import urllib.request
import numpy as np
import pandas as pd
import streamlit as st
import jaydebeapi


st.set_page_config(
    page_title="SWAT2012 MDB Online Editor",
    page_icon="🗂️",
    layout="wide"
)


st.title("🗂️ SWAT2012.mdb Online Reader and Editor")

st.warning(
    "Always keep a backup of the original SWAT2012.mdb. "
    "Edit only user tables such as userwgn, crop, urban, usersoil, etc. "
    "Do not edit MSys or ESRI system tables."
)


# ------------------------------------------------------------
# Helper functions
# ------------------------------------------------------------

def quote_access_name(name):
    return "[" + str(name).replace("]", "]]") + "]"


def safe_filename(name):
    name = str(name)
    name = re.sub(r"[^A-Za-z0-9_\\-\\.]+", "_", name)
    return name


def get_ucanaccess_jars():
    """
    Use existing UCanAccess JAR from lib/.
    If not found, automatically download the UCanAccess uber JAR.
    """

    os.makedirs("lib", exist_ok=True)

    jars = glob.glob("lib/*.jar")

    if len(jars) > 0:
        return jars

    st.info("UCanAccess JAR not found in lib/. Downloading automatically...")

    ucanaccess_url = (
        "https://repo1.maven.org/maven2/io/github/spannm/"
        "ucanaccess/5.1.5/ucanaccess-5.1.5-uber.jar"
    )

    jar_path = "lib/ucanaccess-5.1.5-uber.jar"

    try:
        urllib.request.urlretrieve(ucanaccess_url, jar_path)
    except Exception as e:
        st.error(f"Could not download UCanAccess JAR: {e}")
        st.stop()

    return [jar_path]


def connect_access_db(mdb_path):
    jars = get_ucanaccess_jars()

    jdbc_url = f"jdbc:ucanaccess://{mdb_path};memory=false;showSchema=true"

    conn = jaydebeapi.connect(
        "net.ucanaccess.jdbc.UcanaccessDriver",
        jdbc_url,
        ["", ""],
        jars
    )

    return conn


def list_tables(conn):
    tables = []

    meta = conn.jconn.getMetaData()
    rs = meta.getTables(None, None, "%", None)

    while rs.next():
        table_name = rs.getString("TABLE_NAME")
        table_type = rs.getString("TABLE_TYPE")

        if table_type and table_type.upper() == "TABLE":
            if not table_name.startswith("MSys"):
                tables.append(table_name)

    rs.close()

    return sorted(tables)


def read_table(conn, table_name, limit=None):
    cur = conn.cursor()

    sql = f"SELECT * FROM {quote_access_name(table_name)}"

    if limit is not None:
        sql = f"SELECT TOP {int(limit)} * FROM {quote_access_name(table_name)}"

    cur.execute(sql)

    cols = [c[0] for c in cur.description]
    rows = cur.fetchall()

    cur.close()

    return pd.DataFrame(rows, columns=cols)


def get_column_info(conn, table_name):
    meta = conn.jconn.getMetaData()
    rs = meta.getColumns(None, None, table_name, "%")

    info = []

    while rs.next():
        info.append({
            "column": rs.getString("COLUMN_NAME"),
            "type": rs.getString("TYPE_NAME"),
            "nullable": rs.getString("IS_NULLABLE")
        })

    rs.close()

    return pd.DataFrame(info)


def convert_value(v):
    """
    Convert pandas/numpy values into normal Python values
    before sending to Access database.
    """

    if pd.isna(v):
        return None

    if isinstance(v, str):
        if v.strip() == "":
            return None
        return v

    if isinstance(v, np.integer):
        return int(v)

    if isinstance(v, np.floating):
        return float(v)

    if isinstance(v, np.bool_):
        return bool(v)

    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()

    return v


def is_blank(v):
    if pd.isna(v):
        return True

    if isinstance(v, str) and v.strip() == "":
        return True

    return False


def values_are_different(a, b):
    if pd.isna(a) and pd.isna(b):
        return False

    if is_blank(a) and is_blank(b):
        return False

    return str(a) != str(b)


def save_table_changes_allow_new_rows(conn, table_name, edited_df, original_df, key_col):
    """
    Update existing rows and insert newly added rows.

    Existing rows:
        Identified using key_col.

    New rows:
        Rows whose key_col value is blank or not present in original_df.

    Deleted rows:
        Ignored safely. This function does not delete rows from MDB.
    """

    if key_col not in edited_df.columns:
        raise ValueError("Selected key column not found in edited table.")

    cur = conn.cursor()

    original_keys = set(
        original_df[key_col]
        .dropna()
        .astype(str)
        .tolist()
    )

    editable_columns = list(edited_df.columns)

    updated_rows = 0
    inserted_rows = 0
    skipped_blank_rows = 0

    for _, edited_row in edited_df.iterrows():

        key_value = edited_row[key_col]

        # Ignore completely blank new rows from the Streamlit editor
        row_has_any_value = False
        for col in editable_columns:
            if not is_blank(edited_row[col]):
                row_has_any_value = True
                break

        if not row_has_any_value:
            skipped_blank_rows += 1
            continue

        is_new_row = (
            is_blank(key_value)
            or str(key_value) not in original_keys
        )

        # ----------------------------------------------------
        # INSERT NEW ROW
        # ----------------------------------------------------
        if is_new_row:

            insert_cols = []
            insert_vals = []

            for col in editable_columns:

                val = edited_row[col]

                # If key is blank, skip it.
                # This is useful for AutoNumber fields.
                if col == key_col and is_blank(val):
                    continue

                # Skip blank cells during insert.
                # Access will apply default/null where allowed.
                if is_blank(val):
                    continue

                insert_cols.append(col)
                insert_vals.append(convert_value(val))

            if len(insert_cols) == 0:
                skipped_blank_rows += 1
                continue

            col_clause = ", ".join([quote_access_name(c) for c in insert_cols])
            placeholders = ", ".join(["?"] * len(insert_cols))

            sql = (
                f"INSERT INTO {quote_access_name(table_name)} "
                f"({col_clause}) VALUES ({placeholders})"
            )

            cur.execute(sql, insert_vals)
            inserted_rows += 1

        # ----------------------------------------------------
        # UPDATE EXISTING ROW
        # ----------------------------------------------------
        else:

            matching_original = original_df[
                original_df[key_col].astype(str) == str(key_value)
            ]

            if matching_original.empty:
                continue

            original_row = matching_original.iloc[0]

            update_cols = [c for c in editable_columns if c != key_col]

            changed_cols = []

            for col in update_cols:
                edited_val = edited_row[col]
                original_val = original_row[col]

                if values_are_different(edited_val, original_val):
                    changed_cols.append(col)

            if len(changed_cols) == 0:
                continue

            set_clause = ", ".join([
                f"{quote_access_name(c)} = ?" for c in changed_cols
            ])

            sql = (
                f"UPDATE {quote_access_name(table_name)} "
                f"SET {set_clause} "
                f"WHERE {quote_access_name(key_col)} = ?"
            )

            params = [convert_value(edited_row[c]) for c in changed_cols]
            params.append(convert_value(key_value))

            cur.execute(sql, params)
            updated_rows += 1

    conn.commit()
    cur.close()

    return updated_rows, inserted_rows, skipped_blank_rows


# ------------------------------------------------------------
# Upload MDB
# ------------------------------------------------------------

uploaded_mdb = st.file_uploader(
    "Upload SWAT2012.mdb",
    type=["mdb", "accdb"]
)

if uploaded_mdb is None:
    st.info("Upload your SWAT2012.mdb file to start.")
    st.stop()


# ------------------------------------------------------------
# Save uploaded MDB into working folder
# ------------------------------------------------------------

if "workdir" not in st.session_state:
    st.session_state.workdir = tempfile.mkdtemp()

workdir = st.session_state.workdir

original_path = os.path.join(workdir, safe_filename(uploaded_mdb.name))
edited_path = os.path.join(workdir, "SWAT2012_edited.mdb")

# Save uploaded file only once per upload session
uploaded_signature = f"{uploaded_mdb.name}_{uploaded_mdb.size}"

if st.session_state.get("uploaded_signature") != uploaded_signature:
    st.session_state["uploaded_signature"] = uploaded_signature

    with open(original_path, "wb") as f:
        f.write(uploaded_mdb.getbuffer())

    # Work only on a copy
    shutil.copy(original_path, edited_path)

    # Clear previous table state when new MDB is uploaded
    if "saved_message" in st.session_state:
        del st.session_state["saved_message"]


# ------------------------------------------------------------
# Connect and list tables
# ------------------------------------------------------------

try:
    conn = connect_access_db(edited_path)
except Exception as e:
    st.error(f"Could not open MDB file using UCanAccess: {e}")
    st.stop()


try:
    tables = list_tables(conn)
except Exception as e:
    conn.close()
    st.error(f"Could not list tables: {e}")
    st.stop()


st.success(f"MDB opened successfully. Tables found: {len(tables)}")


# ------------------------------------------------------------
# Table selection
# ------------------------------------------------------------

table_name = st.selectbox(
    "Select table to view/edit",
    options=tables
)

col1, col2 = st.columns([2, 1])

with col1:
    max_rows = st.number_input(
        "Rows to load for editing",
        min_value=10,
        max_value=500000,
        value=1000,
        step=100
    )

with col2:
    load_full_table = st.checkbox("Load full table", value=False)


# ------------------------------------------------------------
# Read table
# ------------------------------------------------------------

if load_full_table:
    df = read_table(conn, table_name, limit=None)
else:
    df = read_table(conn, table_name, limit=max_rows)

column_info = get_column_info(conn, table_name)

st.subheader(f"📋 Table: {table_name}")

with st.expander("Column information"):
    st.dataframe(column_info, use_container_width=True)

st.write(f"Loaded rows: **{len(df)}** | Columns: **{len(df.columns)}**")

st.info(
    "You can now add new rows at the bottom of the table. "
    "For AutoNumber key fields, keep the key value blank in the new row. "
    "Deleted rows are ignored and will not be deleted from the MDB."
)


# ------------------------------------------------------------
# Key column selection
# ------------------------------------------------------------

possible_keys = list(df.columns)

preferred_keys = [
    "OBJECTID",
    "ObjectID",
    "objectid",
    "ID",
    "Id",
    "id",
    "OID",
    "Name",
    "NAME",
    "name",
    "Station",
    "STATION",
    "station"
]

default_key_index = 0

for k in preferred_keys:
    if k in possible_keys:
        default_key_index = possible_keys.index(k)
        break

key_col = st.selectbox(
    "Select key column for safe row updates",
    options=possible_keys,
    index=default_key_index,
    help=(
        "This column is used to identify existing rows during update. "
        "For AutoNumber fields, keep this key blank in newly added rows."
    )
)


# ------------------------------------------------------------
# Edit table online
# ------------------------------------------------------------

st.subheader("✏️ Edit table online")

edited_df = st.data_editor(
    df,
    use_container_width=True,
    num_rows="dynamic",
    height=550
)


# ------------------------------------------------------------
# Save changes
# ------------------------------------------------------------

st.markdown("---")

c1, c2, c3 = st.columns(3)

with c1:
    save_button = st.button("💾 Save changes into MDB copy", type="primary")

with c2:
    csv_data = edited_df.to_csv(index=False).encode("utf-8")
    st.download_button(
        "⬇️ Download edited table CSV",
        data=csv_data,
        file_name=f"{safe_filename(table_name)}_edited.csv",
        mime="text/csv"
    )

with c3:
    excel_path = os.path.join(workdir, f"{safe_filename(table_name)}_edited.xlsx")
    edited_df.to_excel(excel_path, index=False)

    with open(excel_path, "rb") as f:
        st.download_button(
            "⬇️ Download edited table Excel",
            data=f,
            file_name=f"{safe_filename(table_name)}_edited.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )


if save_button:
    try:
        updated_count, inserted_count, skipped_blank_rows = save_table_changes_allow_new_rows(
            conn=conn,
            table_name=table_name,
            edited_df=edited_df,
            original_df=df,
            key_col=key_col
        )

        st.session_state["saved_message"] = (
            f"Saved changes successfully. "
            f"Updated rows: {updated_count}; "
            f"Inserted new rows: {inserted_count}; "
            f"Skipped blank rows: {skipped_blank_rows}."
        )

        st.success(st.session_state["saved_message"])

    except Exception as e:
        st.error(f"Could not save changes: {e}")


if "saved_message" in st.session_state:
    st.success(st.session_state["saved_message"])


# ------------------------------------------------------------
# Download edited MDB
# ------------------------------------------------------------

st.subheader("📦 Download edited MDB")

try:
    conn.close()
except Exception:
    pass

with open(edited_path, "rb") as f:
    st.download_button(
        "Download SWAT2012_edited.mdb",
        data=f,
        file_name="SWAT2012_edited.mdb",
        mime="application/octet-stream",
        type="primary"
    )


st.info(
    "After download, keep the original SWAT2012.mdb as backup. "
    "Then replace the ArcSWAT database only after testing the edited MDB."
)
