"""Labs: analytes (seeded), lab reports, lab results, lab extractions.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-08

Additive only (DESIGN §5.5): four new tables, plus seed rows for the common
analytes with their Spanish aliases (reference data, no PHI). No existing row
is touched. ``lab_links`` belongs to a later ticket.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SPECIMENS = ("sangre", "orina", "heces", "otro")
REPORT_SOURCES = ("whatsapp", "upload", "link")
REPORT_STATUSES = ("ok", "needs_review", "failed")
PATIENT_MATCHES = ("cip", "name", "manual")
FLAGS = ("alto", "bajo", "normal", "anormal")
COMPARATORS = ("<", ">", "<=", ">=")
EXTRACTION_OUTCOMES = ("lab_report", "not_lab_report", "failed")

# (canonical_name, aliases, default_unit, category, specimen). Matching is on
# the normalized form (accents, case, punctuation, whitespace) of the name and
# every alias, within the same specimen; keys must be unique per specimen
# (tests/test_labs.py checks it).
HEMOGRAMA = "Hemograma"
QUIMICA = "Química"
LIPIDOS = "Perfil lipídico"
HEPATICO = "Perfil hepático"
RENAL = "Función renal"
ELECTROLITOS = "Electrolitos"
TIROIDES = "Perfil tiroideo"
VITAMINAS = "Vitaminas y hierro"
PROSTATA = "Próstata"
COAGULACION = "Coagulación"
INFLAMACION = "Inflamación"
ORINA = "Urinálisis"

ANALYTES: list[tuple[str, list[str], str | None, str, str]] = [
    # Hemograma
    ("Hemoglobina", ["Hb", "HGB", "Hemoglobina total"], "g/dL", HEMOGRAMA, "sangre"),
    ("Hematocrito", ["Hto", "HCT"], "%", HEMOGRAMA, "sangre"),
    (
        "Glóbulos rojos",
        ["Eritrocitos", "RBC", "Hematíes", "Recuento de glóbulos rojos", "Recuento de eritrocitos"],
        "millones/µL",
        HEMOGRAMA,
        "sangre",
    ),
    (
        "Glóbulos blancos",
        ["Leucocitos", "WBC", "Recuento de glóbulos blancos", "Recuento de leucocitos"],
        "10³/µL",
        HEMOGRAMA,
        "sangre",
    ),
    (
        "Plaquetas",
        ["PLT", "Trombocitos", "Recuento de plaquetas"],
        "10³/µL",
        HEMOGRAMA,
        "sangre",
    ),
    ("VCM", ["Volumen corpuscular medio", "MCV"], "fL", HEMOGRAMA, "sangre"),
    ("HCM", ["Hemoglobina corpuscular media", "MCH"], "pg", HEMOGRAMA, "sangre"),
    (
        "CHCM",
        ["Concentración de hemoglobina corpuscular media", "MCHC"],
        "g/dL",
        HEMOGRAMA,
        "sangre",
    ),
    (
        "ADE",
        ["RDW", "RDW-CV", "Amplitud de distribución eritrocitaria"],
        "%",
        HEMOGRAMA,
        "sangre",
    ),
    ("VPM", ["MPV", "Volumen plaquetario medio"], "fL", HEMOGRAMA, "sangre"),
    (
        "Neutrófilos",
        ["Neutrófilos segmentados", "Segmentados", "NEU%", "Neutrófilos %"],
        "%",
        HEMOGRAMA,
        "sangre",
    ),
    ("Linfocitos", ["LYM%", "LINF%", "Linfocitos %"], "%", HEMOGRAMA, "sangre"),
    ("Monocitos", ["MONO%", "Monocitos %"], "%", HEMOGRAMA, "sangre"),
    ("Eosinófilos", ["EOS%", "Eosinófilos %"], "%", HEMOGRAMA, "sangre"),
    ("Basófilos", ["BASO%", "Basófilos %"], "%", HEMOGRAMA, "sangre"),
    (
        "Neutrófilos absolutos",
        ["NEU#", "Neutrófilos #", "Recuento absoluto de neutrófilos"],
        "10³/µL",
        HEMOGRAMA,
        "sangre",
    ),
    (
        "Linfocitos absolutos",
        ["LYM#", "Linfocitos #", "Recuento absoluto de linfocitos"],
        "10³/µL",
        HEMOGRAMA,
        "sangre",
    ),
    # Química
    (
        "Glucosa",
        ["Glucosa en ayunas", "Glicemia", "Glucemia", "Glucosa basal", "Glucosa sérica", "GLU"],
        "mg/dL",
        QUIMICA,
        "sangre",
    ),
    (
        "Hemoglobina glicosilada (HbA1c)",
        [
            "HbA1c",
            "A1c",
            "Hemoglobina glicosilada",
            "Hemoglobina glicada",
            "Hemoglobina glucosilada",
            "Hemoglobina A1c",
        ],
        "%",
        QUIMICA,
        "sangre",
    ),
    ("Ácido úrico", ["AU", "Uricemia"], "mg/dL", QUIMICA, "sangre"),
    (
        "Proteínas totales",
        ["Proteína total", "Proteínas totales séricas"],
        "g/dL",
        QUIMICA,
        "sangre",
    ),
    ("Albúmina", ["ALB", "Albúmina sérica"], "g/dL", QUIMICA, "sangre"),
    # Función renal
    ("Creatinina", ["Creatinina sérica", "CREA", "Creatinina en suero"], "mg/dL", RENAL, "sangre"),
    (
        "Nitrógeno ureico (BUN)",
        ["BUN", "Nitrógeno ureico", "Nitrógeno de urea", "Nitrógeno ureico en sangre"],
        "mg/dL",
        RENAL,
        "sangre",
    ),
    ("Urea", ["Urea sérica"], "mg/dL", RENAL, "sangre"),
    (
        "Filtración glomerular estimada",
        ["TFG", "TFGe", "TFG estimada", "eGFR", "Tasa de filtración glomerular"],
        "mL/min/1.73m²",
        RENAL,
        "sangre",
    ),
    # Electrolitos
    ("Sodio", ["Na", "Sodio sérico"], "mmol/L", ELECTROLITOS, "sangre"),
    ("Potasio", ["K", "Potasio sérico"], "mmol/L", ELECTROLITOS, "sangre"),
    ("Cloro", ["Cl", "Cloruro", "Cloruros"], "mmol/L", ELECTROLITOS, "sangre"),
    ("Calcio", ["Ca", "Calcio total", "Calcio sérico"], "mg/dL", ELECTROLITOS, "sangre"),
    ("Magnesio", ["Mg", "Magnesio sérico"], "mg/dL", ELECTROLITOS, "sangre"),
    ("Fósforo", ["P", "Fosfato", "Fósforo inorgánico"], "mg/dL", ELECTROLITOS, "sangre"),
    # Perfil lipídico
    ("Colesterol total", ["Colesterol", "CHOL", "Colesterol sérico"], "mg/dL", LIPIDOS, "sangre"),
    (
        "Colesterol HDL",
        ["HDL", "HDL colesterol", "HDL-C", "Colesterol de alta densidad"],
        "mg/dL",
        LIPIDOS,
        "sangre",
    ),
    (
        "Colesterol LDL",
        ["LDL", "LDL colesterol", "LDL-C", "LDL calculado", "Colesterol de baja densidad"],
        "mg/dL",
        LIPIDOS,
        "sangre",
    ),
    ("Colesterol VLDL", ["VLDL", "VLDL colesterol"], "mg/dL", LIPIDOS, "sangre"),
    ("Triglicéridos", ["TG", "TRIG"], "mg/dL", LIPIDOS, "sangre"),
    # Perfil hepático
    ("Bilirrubina total", ["Bilirrubina", "BT"], "mg/dL", HEPATICO, "sangre"),
    ("Bilirrubina directa", ["BD", "Bilirrubina conjugada"], "mg/dL", HEPATICO, "sangre"),
    ("Bilirrubina indirecta", ["BI", "Bilirrubina no conjugada"], "mg/dL", HEPATICO, "sangre"),
    (
        "AST (TGO)",
        ["AST", "TGO", "SGOT", "Aspartato aminotransferasa"],
        "U/L",
        HEPATICO,
        "sangre",
    ),
    ("ALT (TGP)", ["ALT", "TGP", "SGPT", "Alanina aminotransferasa"], "U/L", HEPATICO, "sangre"),
    ("Fosfatasa alcalina", ["FA", "ALP"], "U/L", HEPATICO, "sangre"),
    (
        "GGT",
        ["Gamma GT", "GGTP", "Gamma glutamil transferasa", "Gamma glutamiltransferasa"],
        "U/L",
        HEPATICO,
        "sangre",
    ),
    # Perfil tiroideo
    (
        "TSH",
        ["Tirotropina", "TSH ultrasensible", "Hormona estimulante de la tiroides"],
        "µUI/mL",
        TIROIDES,
        "sangre",
    ),
    ("T4 libre", ["T4L", "FT4", "Tiroxina libre"], "ng/dL", TIROIDES, "sangre"),
    ("T3 libre", ["T3L", "FT3", "Triyodotironina libre"], "pg/mL", TIROIDES, "sangre"),
    # Vitaminas y hierro
    (
        "Vitamina B12",
        ["B12", "VIT B12", "Vitamina B 12", "Cobalamina", "Cianocobalamina"],
        "pg/mL",
        VITAMINAS,
        "sangre",
    ),
    (
        "Vitamina D 25-OH",
        [
            "Vitamina D",
            "Vitamina D total",
            "25-OH vitamina D",
            "25 hidroxi vitamina D",
            "25-hidroxivitamina D",
            "25(OH)D",
        ],
        "ng/mL",
        VITAMINAS,
        "sangre",
    ),
    ("Ácido fólico", ["Folato", "Folatos", "Folato sérico"], "ng/mL", VITAMINAS, "sangre"),
    ("Hierro sérico", ["Hierro", "Fe"], "µg/dL", VITAMINAS, "sangre"),
    ("Ferritina", ["Ferritina sérica"], "ng/mL", VITAMINAS, "sangre"),
    # Próstata
    (
        "PSA total",
        ["PSA", "Antígeno prostático específico", "Antígeno prostático específico total"],
        "ng/mL",
        PROSTATA,
        "sangre",
    ),
    ("PSA libre", ["fPSA", "Antígeno prostático específico libre"], "ng/mL", PROSTATA, "sangre"),
    # Coagulación
    ("Tiempo de protrombina", ["TP", "PT"], "s", COAGULACION, "sangre"),
    ("INR", ["Razón normalizada internacional"], None, COAGULACION, "sangre"),
    # Inflamación
    ("Proteína C reactiva", ["PCR", "CRP", "PCR cuantitativa"], "mg/L", INFLAMACION, "sangre"),
    (
        "Velocidad de sedimentación",
        ["VSG", "VES", "Eritrosedimentación"],
        "mm/h",
        INFLAMACION,
        "sangre",
    ),
    # Urinálisis (matched only against results the lab reports on urine)
    ("Color de orina", ["Color"], None, ORINA, "orina"),
    ("Aspecto de orina", ["Aspecto"], None, ORINA, "orina"),
    ("Densidad urinaria", ["Densidad", "Gravedad específica"], None, ORINA, "orina"),
    ("pH urinario", ["pH"], None, ORINA, "orina"),
    ("Proteínas en orina", ["Proteínas", "Proteína"], None, ORINA, "orina"),
    ("Glucosa en orina", ["Glucosa", "Glucosuria"], None, ORINA, "orina"),
    ("Cetonas en orina", ["Cetonas", "Cuerpos cetónicos"], None, ORINA, "orina"),
    ("Sangre en orina", ["Sangre", "Hemoglobina", "Sangre oculta"], None, ORINA, "orina"),
    ("Nitritos", [], None, ORINA, "orina"),
    (
        "Leucocitos en orina",
        ["Leucocitos", "Esterasa leucocitaria", "Glóbulos blancos"],
        None,
        ORINA,
        "orina",
    ),
    ("Eritrocitos en orina", ["Eritrocitos", "Glóbulos rojos", "Hematíes"], None, ORINA, "orina"),
    ("Bilirrubina en orina", ["Bilirrubina"], None, ORINA, "orina"),
    ("Urobilinógeno", [], None, ORINA, "orina"),
    ("Bacterias en orina", ["Bacterias"], None, ORINA, "orina"),
    ("Células epiteliales en orina", ["Células epiteliales"], None, ORINA, "orina"),
]


def _in(column: str, values: tuple[str, ...], *, nullable: bool = False) -> str:
    check = f"{column} IN (" + ", ".join(f"'{v}'" for v in values) + ")"
    return f"{column} IS NULL OR {check}" if nullable else check


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def _authors() -> list[sa.Column]:
    return [
        sa.Column(
            "created_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_by",
            sa.BigInteger(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    ]


def upgrade() -> None:
    analytes = op.create_table(
        "analytes",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("canonical_name", sa.String(200), nullable=False, unique=True),
        sa.Column(
            "aliases",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("default_unit", sa.String(50), nullable=True),
        sa.Column("category", sa.String(50), nullable=True),
        sa.Column("specimen", sa.String(10), nullable=False, server_default="sangre"),
        sa.Column("needs_review", sa.Boolean(), nullable=False, server_default="false"),
        *_timestamps(),
        *_authors(),
        sa.CheckConstraint(_in("specimen", SPECIMENS), name="ck_analytes_specimen"),
    )

    op.create_table(
        "lab_reports",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "patient_id",
            sa.BigInteger(),
            sa.ForeignKey("patients.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("collected_on", sa.Date(), nullable=True),
        sa.Column("reported_on", sa.Date(), nullable=True),
        sa.Column("lab_name", sa.String(200), nullable=True),
        sa.Column(
            "attachment_id",
            sa.BigInteger(),
            sa.ForeignKey("attachments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column(
            "source_message_id",
            sa.BigInteger(),
            sa.ForeignKey("whatsapp_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="needs_review"),
        sa.Column("patient_match", sa.String(10), nullable=True),
        sa.Column("raw_extraction", postgresql.JSONB(), nullable=True),
        *_timestamps(),
        *_authors(),
        sa.CheckConstraint(_in("source", REPORT_SOURCES), name="ck_lab_reports_source"),
        sa.CheckConstraint(_in("status", REPORT_STATUSES), name="ck_lab_reports_status"),
        sa.CheckConstraint(
            _in("patient_match", PATIENT_MATCHES, nullable=True),
            name="ck_lab_reports_patient_match",
        ),
    )
    op.create_index(
        "ix_lab_reports_patient_collected", "lab_reports", ["patient_id", "collected_on"]
    )
    op.create_index("ix_lab_reports_attachment_id", "lab_reports", ["attachment_id"])
    op.create_index("ix_lab_reports_source_message_id", "lab_reports", ["source_message_id"])

    op.create_table(
        "lab_results",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "report_id",
            sa.BigInteger(),
            sa.ForeignKey("lab_reports.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "analyte_id",
            sa.BigInteger(),
            sa.ForeignKey("analytes.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("panel", sa.String(200), nullable=True),
        sa.Column("raw_name", sa.String(300), nullable=False),
        sa.Column("value_text", sa.String(300), nullable=True),
        sa.Column("value_num", sa.Numeric(), nullable=True),
        sa.Column("comparator", sa.String(2), nullable=True),
        sa.Column("unit", sa.String(50), nullable=True),
        sa.Column("ref_low", sa.Numeric(), nullable=True),
        sa.Column("ref_high", sa.Numeric(), nullable=True),
        sa.Column("ref_text", sa.String(300), nullable=True),
        sa.Column("flag", sa.String(10), nullable=True),
        *_timestamps(),
        *_authors(),
        sa.CheckConstraint(_in("flag", FLAGS, nullable=True), name="ck_lab_results_flag"),
        sa.CheckConstraint(
            _in("comparator", COMPARATORS, nullable=True), name="ck_lab_results_comparator"
        ),
    )
    op.create_index("ix_lab_results_report_id", "lab_results", ["report_id"])
    op.create_index("ix_lab_results_analyte_id", "lab_results", ["analyte_id"])

    op.create_table(
        "lab_extractions",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "attachment_id",
            sa.BigInteger(),
            sa.ForeignKey("attachments.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "report_id",
            sa.BigInteger(),
            sa.ForeignKey("lab_reports.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("error", sa.String(50), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column(
            "usage", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        *_timestamps(),
        sa.CheckConstraint(_in("outcome", EXTRACTION_OUTCOMES), name="ck_lab_extractions_outcome"),
    )

    op.bulk_insert(
        analytes,
        [
            {
                "canonical_name": name,
                "aliases": aliases,
                "default_unit": unit,
                "category": category,
                "specimen": specimen,
                "needs_review": False,
            }
            for name, aliases, unit, category, specimen in ANALYTES
        ],
    )


def downgrade() -> None:
    op.drop_table("lab_extractions")
    op.drop_index("ix_lab_results_analyte_id", table_name="lab_results")
    op.drop_index("ix_lab_results_report_id", table_name="lab_results")
    op.drop_table("lab_results")
    op.drop_index("ix_lab_reports_source_message_id", table_name="lab_reports")
    op.drop_index("ix_lab_reports_attachment_id", table_name="lab_reports")
    op.drop_index("ix_lab_reports_patient_collected", table_name="lab_reports")
    op.drop_table("lab_reports")
    op.drop_table("analytes")
