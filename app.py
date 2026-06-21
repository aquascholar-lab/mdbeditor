import os
import re
import glob
import shutil
import tempfile
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
    jars = glob.glob("lib/*.jar")

    if len(jars) == 0:
        st.error(
            "No UCanAccess JAR files found. Please create a lib folder and place all UCanAccess .jar files inside it."
        )
        st.stop()

    return jars


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
    if pd.isna(v):
        return None

    if isinstance(v, np.integer):
        return int(v)

    if isinstance(v, np.floating):
        return float(v)

    if isinstance(v, np.bool_):
        return bool(v)

    return v


def update_table_by_key(conn, table_name, edited_df, original_df, key_col):
    """
    Updates existing rows only.
    Does not add or delete rows.
    """

    if key_col not in edited_df.columns:
        raise ValueError("Selected key column not found in edited table.")

    if len(edited_df) != len(original_df):
        raise ValueError(
            "Row count changed. For safe MDB editing, do not add/delete rows. Edit values only."
        )

    cur = conn.cursor()

    update_cols = [c for c in edited_df.columns if c != key_col]

    updated_rows = 0

    for i in range(len(edited_df)):
        edited_row = edited_df.iloc[i]
        original_row = original_df.iloc[i]

        # Skip rows that are unchanged
        changed = False
        for col in update_cols:
            ev = edited_row[col]
            ov = original_row[col]

            if pd.isna(ev) and pd.isna(ov):
                continue

            if str(ev) != str(ov):
                changed = True
                break

        if not changed:
            continue

        set_clause = ", ".join([f"{quote_access_name(c)} = ?" for c in update_cols])

        sql = (
            f"UPDATE {quote_access_name(table_name)} "
            f"SET {set_clause} "
            f"WHERE {quote_access_name(key_col)} = ?"
        )

        params = [convert_value(edited_row[c]) for c in update_cols]
        params.append(convert_value(edited_row[key_col]))

        cur.execute(sql, params)

        updated_rows += 1

    conn.commit()
    cur.close()

    return updated_rows


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

with open(original_path, "wb") as f:
    f.write(uploaded_mdb.getbuffer())

# Work only on a copy
shutil.copy(original_path, edited_path)


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
        max_value=100000,
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


# ------------------------------------------------------------
# Key column selection
# ------------------------------------------------------------

possible_keys = list(df.columns)

preferred_keys = [
    "OBJECTID",
    "ID",
    "OID",
    "Name",
    "NAME",
    "Station",
    "STATION"
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
    help="This column is used in the WHERE clause during update. Prefer OBJECTID or ID if available."
)


# ------------------------------------------------------------
# Edit table online
# ------------------------------------------------------------

st.subheader("✏️ Edit table online")

edited_df = st.data_editor(
    df,
    use_container_width=True,
    num_rows="fixed",
    height=500
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
        updated_count = update_table_by_key(
            conn=conn,
            table_name=table_name,
            edited_df=edited_df,
            original_df=df,
            key_col=key_col
        )

        st.success(f"Saved changes successfully. Updated rows: {updated_count}")

    except Exception as e:
        st.error(f"Could not save changes: {e}")


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
