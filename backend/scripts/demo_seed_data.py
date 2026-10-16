"""Synthetic records for ``scripts.demo_seed`` (issue #142).

Every person, clinic, lab, ID number, phone and value here is **invented**
for the public product write-up: no real patient, doctor or institution.
Phones use the reserved-looking +507 6000-00xx range and e-mails
``@example.com``. The patients keep their app display names (Papá / Mamá).

Dates are written on the storyline's own calendar, which ends on
:data:`ANCHOR`; ``demo_seed`` moves all of them forward by whole weeks so the
story always ends "now" (weekday names in the texts stay true).
"""

from datetime import date

# The storyline's "today": the last WhatsApp day is the day before.
ANCHOR = date(2026, 10, 16)

USERS = {
    # key: (email, display name, role, can_import)
    "andres": ("demo@example.com", "Andrés", "admin", True),
    "lucia": ("lucia@example.com", "Lucía", "member", False),
    "marta": ("marta@example.com", "Marta", "admin", False),
}

PATIENTS = {
    "papa": {
        "full_name": "Aurelio Benítez Cardona",
        "id_number": "8-888-1001",
        "birth_date": date(1941, 3, 12),
        "diagnoses": (
            "Enfermedad de Parkinson (diagnóstico 2019). Hipertensión arterial. "
            "Hiperplasia prostática benigna. Déficit de vitamina D. "
            "Enfermedad renal crónica leve (creatinina 1.3–1.4)."
        ),
        "notes": "Usa bastón. Riesgo de caídas: acompañarlo en el baño y las escaleras.",
    },
    "mama": {
        "full_name": "Clotilde Ramírez de Benítez",
        "id_number": "8-888-1002",
        "birth_date": date(1944, 8, 27),
        "diagnoses": (
            "Hipertensión arterial. Hipotiroidismo. Dislipidemia. Osteopenia. "
            "Catarata en ojo derecho. Trombosis venosa profunda de pierna izquierda "
            "(septiembre 2026), anticoagulada con rivaroxabán."
        ),
        "notes": "Alergia a la penicilina (urticaria).",
    },
}

DOCTORS = {
    # key: name, aliases, specialty, phone, email, clinic
    "neuro": (
        "Dr. Mateo Salcedo Fuentes",
        ["Dr. Salcedo"],
        "Neurología",
        "+507 6000-0001",
        "consultorio.salcedo@example.com",
        "Centro Neurológico Bahía Azul",
    ),
    "cardio": (
        "Dra. Valeria Sotomayor Quirós",
        ["Dra. Sotomayor"],
        "Cardiología",
        "+507 6000-0002",
        "citas.sotomayor@example.com",
        "Instituto Cardiovascular Punta Coral",
    ),
    "vascular": (
        "Dr. Esteban Larrinaga Pupo",
        ["Dr. Larrinaga"],
        "Cirugía vascular",
        "+507 6000-0003",
        "dr.larrinaga@example.com",
        "Policlínica Los Almendros",
    ),
    "interna_mama": (
        "Dra. Inés Carvajal Ledezma",
        ["Dra. Carvajal", "Dra. Inés"],
        "Medicina interna",
        "+507 6000-0004",
        "dra.carvajal@example.com",
        "Centro Médico Brisas del Pacífico",
    ),
    "interna_papa": (
        "Dr. Fabián Montenegro Achurra",
        ["Dr. Montenegro"],
        "Medicina interna",
        "+507 6000-0005",
        "dr.montenegro@example.com",
        "Centro Médico Brisas del Pacífico",
    ),
    "oftalmo": (
        "Dra. Lorena Quintanar Baeza",
        ["Dra. Quintanar"],
        "Oftalmología",
        "+507 6000-0006",
        "vistaclara.quintanar@example.com",
        "Centro Oftalmológico Vista Clara",
    ),
    "uro": (
        "Dr. Tomás Echevarría Nájera",
        ["Dr. Echevarría"],
        "Urología",
        "+507 6000-0007",
        "dr.echevarria@example.com",
        "Policlínica Los Almendros",
    ),
    "fisio": (
        "Lic. Daniela Ferrán Uribe",
        ["Lic. Ferrán"],
        "Fisioterapia",
        "+507 6000-0008",
        "fisio.ferran@example.com",
        "Centro de Rehabilitación Marea Alta",
    ),
}

LAB_NAME = "Laboratorio Clínico Arcoíris"
IMAGING = "Imagen Diagnóstica Costa Verde"


def d(text: str) -> date:
    return date.fromisoformat(text)


# ── Bitácora ────────────────────────────────────────────────────────────────
# (date, patient, doctor|None, entry_type, channel, title, indications,
#  family_notes, author, extra) — extra: {"pending": True, "source": "...",
#  "time": "HH:MM", "appt": True (a past appointment marked done), "event":
#  [(kind, medication, detail, pending)]}

ENTRIES = [
    # ── Papá ────────────────────────────────────────────────────────────────
    (
        "2026-04-17",
        "papa",
        "neuro",
        "consulta_control",
        "presencial",
        "Control de Parkinson",
        "Mantener levodopa/carbidopa 25/100: 1½ tabletas en desayuno, almuerzo y cena. "
        "Pramipexol 0.5 mg en la noche y levodopa/carbidopa CR 50/200 al acostarse.\n\n"
        "- Caminar 20 minutos al día, siempre acompañado.\n- Control en 3 meses.",
        "Lo vio bien en la mañana; temblor leve en la mano derecha. Le contamos que se "
        "queda «pegado» al pasar por las puertas.",
        "andres",
        {"appt": True, "time": "10:00"},
    ),
    (
        "2026-04-24",
        "papa",
        "fisio",
        "terapia",
        "presencial",
        "Fisioterapia: evaluación inicial",
        "Ejercicios de marcha con pasos largos y señales en el piso para las puertas. "
        "Dos sesiones por semana.",
        "Le costó el equilibrio con los ojos cerrados.",
        "lucia",
        {"appt": True, "time": "09:00"},
    ),
    (
        "2026-05-02",
        "papa",
        None,
        "observacion_familiar",
        None,
        "Caída leve en el baño",
        None,
        "Se resbaló al salir de la ducha. Sin golpe en la cabeza; raspón en el codo. "
        "Pusimos alfombra antideslizante y una barra junto a la ducha.",
        "marta",
        {},
    ),
    (
        "2026-05-06",
        "papa",
        None,
        "examen",
        "presencial",
        "Laboratorios de rutina",
        "Hemograma, química, PSA, vitamina D y B12. Resultados en Laboratorios.",
        None,
        "andres",
        {"source": LAB_NAME},
    ),
    (
        "2026-05-12",
        "papa",
        "interna_papa",
        "consulta_control",
        "presencial",
        "Revisión de laboratorios",
        "Vitamina D baja (22 ng/mL): iniciar colecalciferol 1000 UI con el desayuno. "
        "Creatinina 1.3: tomar 1.5 litros de agua al día y no usar antiinflamatorios. "
        "Presión 128/76.",
        None,
        "andres",
        {
            "appt": True,
            "time": "15:00",
            "event": [("inicio", "Colecalciferol 1000 UI", "Vitamina D baja", False)],
        },
    ),
    (
        "2026-05-15",
        "papa",
        "fisio",
        "terapia",
        "presencial",
        "Fisioterapia",
        "Marcha, equilibrio y fuerza de piernas.",
        None,
        "lucia",
        {"appt": True, "time": "09:00"},
    ),
    (
        "2026-05-29",
        "papa",
        "fisio",
        "terapia",
        "presencial",
        "Fisioterapia",
        "Ejercicios para levantarse de la silla sin apoyo.",
        "Ya se levanta solo de la silla del comedor.",
        "lucia",
        {"appt": True, "time": "09:00"},
    ),
    (
        "2026-06-03",
        "papa",
        "uro",
        "consulta_control",
        "presencial",
        "Control de próstata",
        "PSA 3.1, estable. Continuar tamsulosina 0.4 mg en la noche. Ecografía prostática "
        "en 6 meses.",
        None,
        "andres",
        {"appt": True, "time": "11:30"},
    ),
    (
        "2026-06-10",
        "papa",
        None,
        "observacion_familiar",
        None,
        "Alucinaciones visuales en la noche",
        None,
        "Dos noches seguidas dice que ve gatos en el cuarto. No se asustó. "
        "Preguntarle al neurólogo.",
        "marta",
        {},
    ),
    (
        "2026-06-11",
        "papa",
        "neuro",
        "teleconsulta",
        "whatsapp",
        "Consulta por alucinaciones",
        "Probable efecto del pramipexol: bajarlo a 0.25 mg por una semana y luego "
        "suspenderlo. Si siguen las alucinaciones, avisar.",
        None,
        "marta",
        {
            "event": [
                ("suspension", "Pramipexol 0.5 mg", "Retiro en una semana por alucinaciones", False)
            ]
        },
    ),
    (
        "2026-06-18",
        "papa",
        "uro",
        "consulta_enfermedad",
        "telefono",
        "Ardor al orinar",
        "Urocultivo. Nitrofurantoína 100 mg cada 12 horas por 5 días.",
        None,
        "andres",
        {"pending": True, "event": [("inicio", "Nitrofurantoína 100 mg", "5 días", True)]},
    ),
    (
        "2026-06-26",
        "papa",
        "fisio",
        "terapia",
        "presencial",
        "Fisioterapia",
        "Marcha con obstáculos y giros.",
        None,
        "lucia",
        {"appt": True, "time": "09:00"},
    ),
    (
        "2026-07-08",
        "papa",
        "neuro",
        "consulta_control",
        "presencial",
        "Control de Parkinson (3 meses)",
        "Sin alucinaciones desde que se suspendió el pramipexol. Lentitud en la tarde "
        "(fin de dosis hacia las 4 p. m.): agregar una tableta de levodopa/carbidopa "
        "25/100 a las 5 p. m. Mantener la CR al acostarse. Control en 2 meses.",
        "Le preguntamos por el sueño: se despierta dos veces en la noche para orinar.",
        "andres",
        {
            "appt": True,
            "time": "10:00",
            "event": [
                (
                    "cambio_dosis",
                    "Levodopa/Carbidopa 25/100",
                    "Agrega 1 tableta a las 5 p. m.",
                    False,
                )
            ],
        },
    ),
    (
        "2026-07-20",
        "papa",
        None,
        "vacuna",
        "presencial",
        "Vacuna contra la influenza",
        "Dosis anual 2026.",
        None,
        "marta",
        {"source": "Centro de Salud Las Palmeras"},
    ),
    (
        "2026-07-24",
        "papa",
        "fisio",
        "terapia",
        "presencial",
        "Fisioterapia",
        "Equilibrio en una pierna con apoyo.",
        None,
        "lucia",
        {"appt": True, "time": "09:00"},
    ),
    (
        "2026-08-07",
        "papa",
        None,
        "observacion_familiar",
        None,
        "Estreñimiento",
        None,
        "Tres días sin evacuar. Se le dio un sobre de polietilenglicol y evacuó al día siguiente.",
        "marta",
        {},
    ),
    (
        "2026-08-12",
        "papa",
        None,
        "examen",
        "presencial",
        "Laboratorios de control",
        "Hemograma, química, vitamina D, B12 y PSA.",
        None,
        "andres",
        {"source": LAB_NAME},
    ),
    (
        "2026-08-18",
        "papa",
        "interna_papa",
        "consulta_control",
        "presencial",
        "Control general",
        "Vitamina D mejoró (31). Creatinina 1.4: repetir en 2 meses y hacer ecografía "
        "renal. Presión 134/80: continuar amlodipino.",
        None,
        "andres",
        {"appt": True, "time": "15:00"},
    ),
    (
        "2026-08-25",
        "papa",
        None,
        "examen",
        "presencial",
        "Ecografía renal",
        "Riñones de tamaño normal. Quistes simples pequeños en el riñón izquierdo. Sin "
        "hidronefrosis.",
        None,
        "lucia",
        {"source": IMAGING},
    ),
    (
        "2026-08-29",
        "papa",
        None,
        "otro",
        None,
        "Retiro de medicamentos en farmacia",
        "Se retiraron 3 meses de levodopa/carbidopa y tamsulosina.",
        None,
        "marta",
        {"pending": True, "source": "Farmacia Central"},
    ),
    (
        "2026-09-04",
        "papa",
        "fisio",
        "terapia",
        "presencial",
        "Fisioterapia",
        "Marcha con bastón y escaleras.",
        None,
        "lucia",
        {"appt": True, "time": "09:00"},
    ),
    (
        "2026-09-09",
        "papa",
        "neuro",
        "consulta_control",
        "presencial",
        "Fluctuaciones motoras",
        "Periodos «off» antes de cada dosis. Agregar entacapona 200 mg junto con la "
        "levodopa de desayuno, almuerzo y cena. Puede aumentar los movimientos "
        "involuntarios: avisar si pasa.",
        "Lo llevamos a las 10; estaba en «off» al llegar y mejoró a la media hora.",
        "andres",
        {
            "appt": True,
            "time": "10:00",
            "event": [
                (
                    "inicio",
                    "Entacapona 200 mg",
                    "Con cada levodopa de desayuno, almuerzo y cena",
                    False,
                )
            ],
        },
    ),
    (
        "2026-09-18",
        "papa",
        None,
        "observacion_familiar",
        None,
        "Movimientos involuntarios leves",
        None,
        "Desde la entacapona tiene movimientos leves de la boca después del almuerzo. "
        "No le molestan.",
        "marta",
        {},
    ),
    (
        "2026-09-22",
        "papa",
        "fisio",
        "terapia",
        "presencial",
        "Fisioterapia",
        "Ejercicios de voz y respiración (programa LSVT).",
        None,
        "lucia",
        {"appt": True, "time": "09:00"},
    ),
    (
        "2026-09-29",
        "papa",
        "fisio",
        "terapia",
        "presencial",
        "Fisioterapia",
        "Marcha y giros; buen avance.",
        None,
        "lucia",
        {"appt": True, "time": "09:00"},
    ),
    (
        "2026-10-01",
        "papa",
        "interna_papa",
        "visita_domiciliaria",
        "domicilio",
        "Visita domiciliaria por tos",
        "Tos seca sin fiebre desde hace 3 días. Pulmones limpios. Líquidos y miel; si "
        "aparece fiebre, avisar.",
        None,
        "marta",
        {"time": "18:30"},
    ),
    (
        "2026-10-07",
        "papa",
        None,
        "examen",
        "presencial",
        "Laboratorios: función renal y vitamina D",
        None,
        None,
        "andres",
        {"source": LAB_NAME},
    ),
    (
        "2026-10-09",
        "papa",
        "interna_papa",
        "teleconsulta",
        "telefono",
        "Resultados de laboratorio",
        "Creatinina 1.3, estable. Vitamina D 38: continuar 1000 UI diarias.",
        None,
        "andres",
        {},
    ),
    (
        "2026-10-11",
        "papa",
        None,
        "observacion_familiar",
        None,
        "Durmió mal",
        None,
        "Se despertó tres veces; en la mañana estaba más lento hasta la segunda dosis.",
        "lucia",
        {"pending": True},
    ),
    (
        "2026-04-29",
        "papa",
        None,
        "observacion_familiar",
        None,
        "Buen día en la finca",
        None,
        "Fuimos a la finca el domingo; caminó con bastón y almorzó con todos.",
        "lucia",
        {},
    ),
    # ── Mamá ────────────────────────────────────────────────────────────────
    (
        "2026-04-22",
        "mama",
        None,
        "examen",
        "presencial",
        "Laboratorios de rutina",
        "Hemograma, química, perfil de lípidos y tiroides.",
        None,
        "marta",
        {"source": LAB_NAME},
    ),
    (
        "2026-04-28",
        "mama",
        "interna_mama",
        "consulta_control",
        "presencial",
        "Control de hipertensión y tiroides",
        "TSH 6.8 (alta): subir levotiroxina de 75 a 88 mcg en ayunas. LDL 142: continuar "
        "atorvastatina 20 mg y dieta. Presión 138/84. Laboratorios en 3 meses.",
        None,
        "marta",
        {
            "appt": True,
            "time": "08:30",
            "event": [("cambio_dosis", "Levotiroxina", "75 → 88 mcg", False)],
        },
    ),
    (
        "2026-05-08",
        "mama",
        "oftalmo",
        "consulta_control",
        "presencial",
        "Control de la vista",
        "Catarata incipiente en el ojo derecho. Presión ocular normal (15/16). Lágrimas "
        "artificiales 3 veces al día. Control en 4 meses.",
        None,
        "lucia",
        {"appt": True, "time": "14:00"},
    ),
    (
        "2026-05-20",
        "mama",
        None,
        "observacion_familiar",
        None,
        "Mareo al levantarse",
        None,
        "Se mareó al levantarse de la cama; presión 105/60 en ese momento. Se recuperó sentada.",
        "lucia",
        {},
    ),
    (
        "2026-05-21",
        "mama",
        "interna_mama",
        "teleconsulta",
        "whatsapp",
        "Mareo al levantarse",
        "Levantarse despacio, en tres tiempos, y tomar más líquidos. No cambiar el "
        "losartán por ahora.",
        None,
        "lucia",
        {},
    ),
    (
        "2026-05-30",
        "mama",
        None,
        "otro",
        "presencial",
        "Limpieza dental",
        "Sin caries nuevas. Control en 6 meses.",
        None,
        "marta",
        {"pending": True, "source": "Consultorio dental Sonrisa Clara"},
    ),
    (
        "2026-06-16",
        "mama",
        None,
        "examen",
        "presencial",
        "Densitometría ósea",
        "Osteopenia en columna lumbar (T −1.8).",
        None,
        "marta",
        {"source": IMAGING},
    ),
    (
        "2026-06-24",
        "mama",
        None,
        "observacion_familiar",
        None,
        "Clases de pintura",
        None,
        "Empezó clases de pintura los martes; está muy animada.",
        "lucia",
        {},
    ),
    (
        "2026-07-15",
        "mama",
        None,
        "examen",
        "presencial",
        "Laboratorios de control",
        "Tiroides, lípidos, glucosa y hemograma.",
        None,
        "marta",
        {"source": LAB_NAME},
    ),
    (
        "2026-07-21",
        "mama",
        "interna_mama",
        "consulta_control",
        "presencial",
        "Revisión de laboratorios",
        "TSH 4.1: mantener levotiroxina 88 mcg. LDL 118. Glucosa 101: dieta. Por la "
        "osteopenia, seguir con calcio + vitamina D.",
        None,
        "marta",
        {"appt": True, "time": "08:30"},
    ),
    (
        "2026-07-30",
        "mama",
        None,
        "vacuna",
        "presencial",
        "Vacuna contra el neumococo",
        "Dosis única.",
        None,
        "lucia",
        {"source": "Centro de Salud Las Palmeras"},
    ),
    (
        "2026-08-13",
        "mama",
        "cardio",
        "consulta_control",
        "presencial",
        "Control cardiológico anual",
        "ECG en ritmo sinusal. Soplo sistólico leve ya conocido. Ecocardiograma: "
        "fracción de eyección 60 %, esclerosis aórtica leve sin estenosis importante. "
        "Presión 132/80. Control en un año.",
        None,
        "marta",
        {"appt": True, "time": "11:00"},
    ),
    (
        "2026-08-27",
        "mama",
        None,
        "observacion_familiar",
        None,
        "Fin de semana en la playa",
        None,
        "Fue con Lucía a la playa; 4 horas de carro de ida y 4 de vuelta.",
        "lucia",
        {},
    ),
    # Storyline: deep vein thrombosis, anticoagulation, cataract surgery.
    (
        "2026-09-01",
        "mama",
        None,
        "observacion_familiar",
        None,
        "Pierna izquierda hinchada",
        None,
        "Desde ayer la pantorrilla izquierda está hinchada, caliente y le duele al "
        "caminar. No tiene falta de aire.",
        "lucia",
        {},
    ),
    (
        "2026-09-02",
        "mama",
        "interna_mama",
        "consulta_enfermedad",
        "presencial",
        "Sospecha de trombosis venosa",
        "Pantorrilla izquierda 3 cm más gruesa que la derecha y dolorosa. Hoy: dímero D, "
        "hemograma, creatinina y TP/INR. Doppler venoso urgente.\n\n"
        "**Si tiene falta de aire o dolor de pecho: urgencias.**",
        "Nos atendió el mismo día al ver la foto de la pierna.",
        "lucia",
        {"appt": True, "time": "16:00"},
    ),
    (
        "2026-09-02",
        "mama",
        None,
        "examen",
        "presencial",
        "Laboratorios urgentes",
        "Dímero D, hemograma, creatinina, TP/INR y PCR.",
        None,
        "lucia",
        {"source": LAB_NAME, "time": "17:30"},
    ),
    (
        "2026-09-03",
        "mama",
        None,
        "examen",
        "presencial",
        "Doppler venoso de pierna izquierda",
        "Trombosis venosa profunda en vena poplítea y femoral superficial izquierdas. "
        "Sistema venoso derecho normal.",
        None,
        "marta",
        {"source": IMAGING, "time": "08:00"},
    ),
    (
        "2026-09-03",
        "mama",
        "vascular",
        "consulta_enfermedad",
        "presencial",
        "Trombosis venosa profunda izquierda",
        "- Rivaroxabán 15 mg cada 12 horas con comida por 21 días; luego 20 mg una vez "
        "al día con la cena.\n- Media de compresión hasta la rodilla durante el día.\n"
        "- Caminar; no estar sentada más de una hora seguida.\n"
        "- **Evitar** ibuprofeno, naproxeno y aspirina.\n- Control en 3 semanas.",
        "El doctor cree que el viaje largo en carro pudo influir.",
        "marta",
        {
            "appt": True,
            "time": "11:00",
            "event": [("inicio", "Rivaroxabán 15 mg", "Cada 12 horas por 21 días", False)],
        },
    ),
    (
        "2026-09-07",
        "mama",
        None,
        "observacion_familiar",
        None,
        "Primeros días con anticoagulante",
        None,
        "Toma bien el rivaroxabán con el desayuno y la cena. Un moretón pequeño en el brazo.",
        "lucia",
        {},
    ),
    (
        "2026-09-11",
        "mama",
        None,
        "observacion_familiar",
        None,
        "Menos hinchazón",
        None,
        "La pierna está menos hinchada y ya camina por la casa sin dolor.",
        "marta",
        {},
    ),
    (
        "2026-09-17",
        "mama",
        None,
        "examen",
        "presencial",
        "Laboratorios de control del anticoagulante",
        "Dímero D, hemograma, creatinina y TP/INR.",
        None,
        "marta",
        {"source": LAB_NAME},
    ),
    (
        "2026-09-18",
        "mama",
        "vascular",
        "teleconsulta",
        "whatsapp",
        "Resultados de control",
        "Dímero D bajando (1.6). Hemoglobina 11.8, levemente baja: vigilar sangrados y "
        "repetir en 2 semanas.",
        None,
        "marta",
        {},
    ),
    (
        "2026-09-24",
        "mama",
        "vascular",
        "consulta_control",
        "presencial",
        "Control de trombosis (3 semanas)",
        "Edema mínimo. Desde hoy rivaroxabán 20 mg una vez al día con la cena, por al "
        "menos 3 meses. Agregar pantoprazol 40 mg en ayunas para proteger el estómago. "
        "Doppler de control en 3 meses.",
        None,
        "marta",
        {
            "appt": True,
            "time": "11:00",
            "event": [
                ("cambio_dosis", "Rivaroxabán", "15 mg cada 12 h → 20 mg una vez al día", False),
                ("inicio", "Pantoprazol 40 mg", "Protección gástrica", True),
            ],
        },
    ),
    (
        "2026-09-26",
        "mama",
        "oftalmo",
        "consulta_control",
        "presencial",
        "Catarata del ojo derecho",
        "La catarata avanzó: agudeza visual 20/80 en el ojo derecho. Se programa "
        "facoemulsificación con lente intraocular, **con sedación**.\n\n"
        "Antes de la cirugía: valoración cardiológica preoperatoria y plan del vascular "
        "para suspender el rivaroxabán.",
        "Nos explicó que la cirugía dura unos 30 minutos y sale el mismo día.",
        "lucia",
        {"appt": True, "time": "14:00"},
    ),
    (
        "2026-09-29",
        "mama",
        "vascular",
        "teleconsulta",
        "whatsapp",
        "Plan del anticoagulante para la cirugía",
        "Suspender rivaroxabán 48 horas antes de la cirugía y reiniciarlo 24 horas "
        "después si no hay sangrado.",
        None,
        "marta",
        {"pending": True},
    ),
    (
        "2026-10-02",
        "mama",
        None,
        "examen",
        "presencial",
        "Laboratorios de control",
        "Dímero D, hemograma, creatinina, TP/INR y TSH.",
        None,
        "marta",
        {"source": LAB_NAME},
    ),
    (
        "2026-10-05",
        "mama",
        None,
        "observacion_familiar",
        None,
        "Sangrado nasal leve",
        None,
        "Sangró de la nariz unos 5 minutos y paró con presión. Le avisamos a la Dra. Carvajal.",
        "lucia",
        {"pending": True},
    ),
    (
        "2026-10-06",
        "mama",
        "interna_mama",
        "teleconsulta",
        "telefono",
        "Sangrado nasal leve",
        "Humidificar el cuarto y vaselina en la nariz. Si se repite o dura más de 20 "
        "minutos: urgencias. No suspender el anticoagulante.",
        None,
        "lucia",
        {},
    ),
    (
        "2026-10-08",
        "mama",
        None,
        "examen",
        "presencial",
        "Electrocardiograma preoperatorio",
        "Ritmo sinusal, 72 latidos por minuto, sin cambios respecto a agosto.",
        None,
        "marta",
        {"source": "Instituto Cardiovascular Punta Coral"},
    ),
    (
        "2026-10-10",
        "mama",
        None,
        "observacion_familiar",
        None,
        "Presión alta en casa",
        None,
        "150/90 y 148/92 en la mañana, dos días seguidos.",
        "lucia",
        {},
    ),
]

# ── Appointments still ahead (on the storyline calendar) ────────────────────
# (date, "HH:MM" | None (all day), patient, doctor|None, reason, location,
#  duration, notes, status)

UPCOMING = [
    # Papá's neurology visit of the WhatsApp week: still «programada» (the
    # family marks it done from the voice-note summary).
    (
        "2026-10-14",
        "10:00",
        "papa",
        "neuro",
        "Control de Parkinson",
        "Centro Neurológico Bahía Azul",
        30,
        None,
        "programada",
    ),
    (
        "2026-10-13",
        "09:00",
        "papa",
        "fisio",
        "Fisioterapia",
        "Centro de Rehabilitación Marea Alta",
        60,
        None,
        "cancelada",
    ),
    (
        "2026-10-20",
        "07:00",
        "mama",
        None,
        "Laboratorios preoperatorios (en ayunas)",
        LAB_NAME,
        30,
        "Hemograma, TP/INR, creatinina y glucosa.",
        "programada",
    ),
    (
        "2026-10-20",
        "09:00",
        "papa",
        "fisio",
        "Fisioterapia",
        "Centro de Rehabilitación Marea Alta",
        60,
        None,
        "programada",
    ),
    (
        "2026-10-22",
        "15:00",
        "papa",
        "interna_papa",
        "Control de presión y función renal",
        "Centro Médico Brisas del Pacífico",
        30,
        None,
        "programada",
    ),
    (
        "2026-10-23",
        "10:30",
        "mama",
        "cardio",
        "Valoración cardiológica preoperatoria (cirugía de catarata con sedación)",
        "Instituto Cardiovascular Punta Coral",
        45,
        "Llevar el electrocardiograma del 8 de octubre, los últimos laboratorios y la lista de "
        "medicamentos.",
        "programada",
    ),
    (
        "2026-10-27",
        "09:00",
        "papa",
        "fisio",
        "Fisioterapia",
        "Centro de Rehabilitación Marea Alta",
        60,
        None,
        "programada",
    ),
    (
        "2026-11-04",
        "07:00",
        "mama",
        "oftalmo",
        "Cirugía de catarata del ojo derecho (facoemulsificación) con sedación",
        "Centro Oftalmológico Vista Clara",
        180,
        "En ayunas desde la medianoche. Rivaroxabán: suspender 48 horas antes, según el Dr. "
        "Larrinaga. Ir acompañada.",
        "programada",
    ),
    (
        "2026-11-05",
        "08:00",
        "mama",
        "oftalmo",
        "Control del día siguiente a la cirugía",
        "Centro Oftalmológico Vista Clara",
        30,
        None,
        "programada",
    ),
    (
        "2026-12-02",
        "11:30",
        "papa",
        "uro",
        "Ecografía prostática y control",
        "Policlínica Los Almendros",
        30,
        None,
        "programada",
    ),
    (
        "2026-11-15",
        None,
        "papa",
        None,
        "Renovar receta de levodopa/carbidopa",
        None,
        30,
        None,
        "programada",
    ),
]

# ── Medication lists: one save per version, keyed items carry their row ────

_PAPA_BASE = {
    "levo_desayuno": dict(
        name="Levodopa/Carbidopa",
        strength="25/100 mg",
        quantity="1½",
        slot="desayuno",
        time="07:30",
        prescriber="neuro",
        reason="Parkinson",
    ),
    "levo_almuerzo": dict(
        name="Levodopa/Carbidopa",
        strength="25/100 mg",
        quantity="1½",
        slot="almuerzo",
        time="12:30",
        prescriber="neuro",
        reason="Parkinson",
    ),
    "levo_cena": dict(
        name="Levodopa/Carbidopa",
        strength="25/100 mg",
        quantity="1½",
        slot="cena",
        time="19:00",
        prescriber="neuro",
        reason="Parkinson",
    ),
    "levo_cr": dict(
        name="Levodopa/Carbidopa CR",
        strength="50/200 mg",
        quantity="1",
        slot="noche",
        time="22:00",
        prescriber="neuro",
        reason="Parkinson (liberación prolongada)",
    ),
    "amlodipino": dict(
        name="Amlodipino",
        strength="5 mg",
        quantity="1",
        slot="desayuno",
        prescriber="interna_papa",
        reason="Presión arterial",
    ),
    "tamsulosina": dict(
        name="Tamsulosina",
        strength="0.4 mg",
        quantity="1",
        slot="noche",
        prescriber="uro",
        reason="Próstata",
    ),
    "peg": dict(
        name="Polietilenglicol 3350",
        strength="17 g",
        quantity="1 sobre",
        slot="segun_necesidad",
        instructions="Disuelto en un vaso de agua",
        reason="Estreñimiento (si pasa 2 días sin evacuar)",
    ),
}

_MAMA_BASE = {
    "levotiroxina": dict(
        name="Levotiroxina",
        strength="75 mcg",
        quantity="1",
        slot="al_despertar",
        time="06:00",
        prescriber="interna_mama",
        instructions="En ayunas, 30 minutos antes del desayuno",
        reason="Hipotiroidismo",
    ),
    "losartan": dict(
        name="Losartán",
        strength="50 mg",
        quantity="1",
        slot="desayuno",
        prescriber="interna_mama",
        reason="Presión arterial",
    ),
    "calcio": dict(
        name="Calcio + Vitamina D3",
        strength="600 mg/400 UI",
        quantity="1",
        slot="almuerzo",
        reason="Huesos",
    ),
    "atorvastatina": dict(
        name="Atorvastatina",
        strength="20 mg",
        quantity="1",
        slot="noche",
        prescriber="interna_mama",
        reason="Colesterol",
    ),
    "lagrimas": dict(
        name="Lágrimas artificiales",
        quantity="1 gota",
        slot="otros",
        schedule_note="En cada ojo, 3 veces al día",
        prescriber="oftalmo",
    ),
    "paracetamol": dict(
        name="Paracetamol",
        strength="500 mg",
        quantity="1",
        slot="segun_necesidad",
        instructions="Máximo 3 al día",
        reason="Dolor",
    ),
}

MED_VERSIONS = {
    "papa": [
        {
            "date": "2026-04-17",
            "summary": "Lista inicial (copiada de la hoja impresa).",
            "items": {
                **_PAPA_BASE,
                "pramipexol": dict(
                    name="Pramipexol",
                    strength="0.5 mg",
                    quantity="1",
                    slot="noche",
                    prescriber="neuro",
                    reason="Parkinson",
                ),
            },
            "avoid": [
                ("Metoclopramida", "Empeora el Parkinson"),
                ("Haloperidol", "Empeora el Parkinson"),
            ],
        },
        {
            "date": "2026-05-12",
            "summary": None,
            "items": {
                **_PAPA_BASE,
                "pramipexol": dict(
                    name="Pramipexol",
                    strength="0.5 mg",
                    quantity="1",
                    slot="noche",
                    prescriber="neuro",
                    reason="Parkinson",
                ),
                "vitd": dict(
                    name="Colecalciferol",
                    strength="1000 UI",
                    quantity="1",
                    slot="desayuno",
                    prescriber="interna_papa",
                    reason="Vitamina D baja",
                ),
            },
            "avoid": [
                ("Metoclopramida", "Empeora el Parkinson"),
                ("Haloperidol", "Empeora el Parkinson"),
                ("Ibuprofeno", "Función renal disminuida"),
            ],
        },
        {
            "date": "2026-07-08",
            "summary": None,
            "items": {
                **_PAPA_BASE,
                "vitd": dict(
                    name="Colecalciferol",
                    strength="1000 UI",
                    quantity="1",
                    slot="desayuno",
                    prescriber="interna_papa",
                    reason="Vitamina D baja",
                ),
                "levo_tarde": dict(
                    name="Levodopa/Carbidopa",
                    strength="25/100 mg",
                    quantity="1",
                    slot="tarde",
                    time="17:00",
                    prescriber="neuro",
                    reason="Fin de dosis en la tarde",
                ),
            },
            "avoid": [
                ("Metoclopramida", "Empeora el Parkinson"),
                ("Haloperidol", "Empeora el Parkinson"),
                ("Ibuprofeno", "Función renal disminuida"),
            ],
        },
        {
            "date": "2026-09-10",
            "summary": None,
            "items": {
                **_PAPA_BASE,
                "vitd": dict(
                    name="Colecalciferol",
                    strength="1000 UI",
                    quantity="1",
                    slot="desayuno",
                    prescriber="interna_papa",
                    reason="Vitamina D baja",
                ),
                "levo_tarde": dict(
                    name="Levodopa/Carbidopa",
                    strength="25/100 mg",
                    quantity="1",
                    slot="tarde",
                    time="17:00",
                    prescriber="neuro",
                    reason="Fin de dosis en la tarde",
                ),
                "enta_desayuno": dict(
                    name="Entacapona",
                    strength="200 mg",
                    quantity="1",
                    slot="desayuno",
                    time="07:30",
                    prescriber="neuro",
                    instructions="Junto con la levodopa",
                ),
                "enta_almuerzo": dict(
                    name="Entacapona",
                    strength="200 mg",
                    quantity="1",
                    slot="almuerzo",
                    time="12:30",
                    prescriber="neuro",
                    instructions="Junto con la levodopa",
                ),
                "enta_cena": dict(
                    name="Entacapona",
                    strength="200 mg",
                    quantity="1",
                    slot="cena",
                    time="19:00",
                    prescriber="neuro",
                    instructions="Junto con la levodopa",
                ),
            },
            "avoid": [
                ("Metoclopramida", "Empeora el Parkinson"),
                ("Haloperidol", "Empeora el Parkinson"),
                ("Ibuprofeno", "Función renal disminuida"),
            ],
        },
    ],
    "mama": [
        {
            "date": "2026-04-22",
            "summary": "Lista inicial (copiada de la hoja impresa).",
            "items": dict(_MAMA_BASE),
            "avoid": [("Penicilina", "Alergia: urticaria")],
        },
        {
            "date": "2026-04-28",
            "summary": None,
            "items": {
                **_MAMA_BASE,
                "levotiroxina": {**_MAMA_BASE["levotiroxina"], "strength": "88 mcg"},
            },
            "avoid": [("Penicilina", "Alergia: urticaria")],
        },
        {
            "date": "2026-09-03",
            "summary": None,
            "items": {
                **_MAMA_BASE,
                "levotiroxina": {**_MAMA_BASE["levotiroxina"], "strength": "88 mcg"},
                "riva15_desayuno": dict(
                    name="Rivaroxabán",
                    strength="15 mg",
                    quantity="1",
                    slot="desayuno",
                    prescriber="vascular",
                    instructions="Con comida",
                    reason="Trombosis venosa",
                    start="2026-09-03",
                    end="2026-09-23",
                ),
                "riva15_cena": dict(
                    name="Rivaroxabán",
                    strength="15 mg",
                    quantity="1",
                    slot="cena",
                    prescriber="vascular",
                    instructions="Con comida",
                    reason="Trombosis venosa",
                    start="2026-09-03",
                    end="2026-09-23",
                ),
                "riva20": dict(
                    name="Rivaroxabán",
                    strength="20 mg",
                    quantity="1",
                    slot="cena",
                    prescriber="vascular",
                    instructions="Con comida",
                    reason="Trombosis venosa",
                    start="2026-09-24",
                ),
                "media": dict(
                    name="Media de compresión (pierna izquierda)",
                    slot="otros",
                    schedule_note="De día; quitarla para dormir",
                    prescriber="vascular",
                ),
            },
            "avoid": [
                ("Penicilina", "Alergia: urticaria"),
                ("Ibuprofeno", "Anticoagulada: riesgo de sangrado"),
                ("Naproxeno", "Anticoagulada: riesgo de sangrado"),
                ("Aspirina", "Anticoagulada: solo si la indica el vascular"),
            ],
        },
        {
            "date": "2026-09-24",
            "summary": None,
            "items": {
                **_MAMA_BASE,
                "levotiroxina": {**_MAMA_BASE["levotiroxina"], "strength": "88 mcg"},
                "riva20": dict(
                    name="Rivaroxabán",
                    strength="20 mg",
                    quantity="1",
                    slot="cena",
                    prescriber="vascular",
                    instructions="Con comida",
                    reason="Trombosis venosa",
                    start="2026-09-24",
                ),
                "media": dict(
                    name="Media de compresión (pierna izquierda)",
                    slot="otros",
                    schedule_note="De día; quitarla para dormir",
                    prescriber="vascular",
                ),
                "pantoprazol": dict(
                    name="Pantoprazol",
                    strength="40 mg",
                    quantity="1",
                    slot="al_despertar",
                    time="06:00",
                    prescriber="vascular",
                    instructions="En ayunas",
                    reason="Protección gástrica (anticoagulante)",
                ),
            },
            "avoid": [
                ("Penicilina", "Alergia: urticaria"),
                ("Ibuprofeno", "Anticoagulada: riesgo de sangrado"),
                ("Naproxeno", "Anticoagulada: riesgo de sangrado"),
                ("Aspirina", "Anticoagulada: solo si la indica el vascular"),
            ],
        },
    ],
}

# ── Labs ────────────────────────────────────────────────────────────────────
# Analytes the seed needs beyond migration 0007's catalogue.
EXTRA_ANALYTES = [
    # canonical, aliases, unit, category
    ("Dímero D", ["D-dímero", "Dimero D", "D-Dimer"], "µg/mL FEU", "Coagulación"),
]

# result: (analyte canonical name, value, unit, ref_low, ref_high, panel)
_HB_F = ("Hemoglobina", "g/dL", 12.0, 16.0, "Hemograma")
_HB_M = ("Hemoglobina", "g/dL", 13.5, 17.5, "Hemograma")


def _r(spec, value):
    name, unit, low, high, panel = spec
    return (name, value, unit, low, high, panel)


DDIMER = ("Dímero D", "µg/mL FEU", None, 0.5, "Coagulación")
INR = ("INR", None, 0.8, 1.2, "Coagulación")
TP = ("Tiempo de protrombina", "s", 10.0, 13.5, "Coagulación")
PLT = ("Plaquetas", "10³/µL", 150, 450, "Hemograma")
WBC = ("Glóbulos blancos", "10³/µL", 4.5, 11.0, "Hemograma")
CREA_F = ("Creatinina", "mg/dL", 0.6, 1.1, "Química")
CREA_M = ("Creatinina", "mg/dL", 0.7, 1.2, "Química")
GLU = ("Glucosa", "mg/dL", 70, 100, "Química")
TSH = ("TSH", "µUI/mL", 0.4, 4.5, "Tiroides")
T4 = ("T4 libre", "ng/dL", 0.8, 1.8, "Tiroides")
LDL = ("Colesterol LDL", "mg/dL", None, 100, "Lípidos")
HDL = ("Colesterol HDL", "mg/dL", 40, None, "Lípidos")
CT = ("Colesterol total", "mg/dL", None, 200, "Lípidos")
TG = ("Triglicéridos", "mg/dL", None, 150, "Lípidos")
PCR = ("Proteína C reactiva", "mg/L", None, 5.0, "Química")
BUN = ("Nitrógeno ureico (BUN)", "mg/dL", 7, 20, "Química")
VITD = ("Vitamina D 25-OH", "ng/mL", 30, 100, "Vitaminas")
B12 = ("Vitamina B12", "pg/mL", 200, 900, "Vitaminas")
PSA = ("PSA total", "ng/mL", None, 4.0, "Marcadores")
NA = ("Sodio", "mmol/L", 135, 145, "Química")
K = ("Potasio", "mmol/L", 3.5, 5.1, "Química")

LAB_REPORTS = [
    # (collected, reported, patient, results)
    (
        "2026-04-22",
        "2026-04-23",
        "mama",
        [
            _r(_HB_F, 12.9),
            _r(PLT, 245),
            _r(WBC, 6.8),
            _r(GLU, 98),
            _r(CREA_F, 0.9),
            _r(TSH, 6.8),
            _r(T4, 0.9),
            _r(CT, 228),
            _r(LDL, 142),
            _r(HDL, 52),
            _r(TG, 160),
        ],
    ),
    (
        "2026-07-15",
        "2026-07-16",
        "mama",
        [
            _r(_HB_F, 12.6),
            _r(PLT, 251),
            _r(GLU, 101),
            _r(CREA_F, 0.9),
            _r(TSH, 4.1),
            _r(T4, 1.1),
            _r(CT, 196),
            _r(LDL, 118),
            _r(HDL, 55),
            _r(TG, 138),
        ],
    ),
    (
        "2026-09-02",
        "2026-09-02",
        "mama",
        [
            _r(DDIMER, 3.8),
            _r(TP, 12.1),
            _r(INR, 1.0),
            _r(_HB_F, 12.4),
            _r(PLT, 238),
            _r(WBC, 8.9),
            _r(CREA_F, 1.0),
            _r(PCR, 8.2),
        ],
    ),
    (
        "2026-09-17",
        "2026-09-18",
        "mama",
        [
            _r(DDIMER, 1.6),
            _r(TP, 14.2),
            _r(INR, 1.3),
            _r(_HB_F, 11.8),
            _r(PLT, 221),
            _r(CREA_F, 1.0),
        ],
    ),
    (
        "2026-10-02",
        "2026-10-03",
        "mama",
        [
            _r(DDIMER, 0.9),
            _r(TP, 13.6),
            _r(INR, 1.2),
            _r(_HB_F, 11.5),
            _r(PLT, 230),
            _r(CREA_F, 1.1),
            _r(TSH, 2.9),
        ],
    ),
    (
        "2026-05-06",
        "2026-05-07",
        "papa",
        [
            _r(_HB_M, 13.4),
            _r(PLT, 198),
            _r(GLU, 104),
            _r(CREA_M, 1.3),
            _r(BUN, 24),
            _r(NA, 138),
            _r(K, 4.4),
            _r(VITD, 22),
            _r(B12, 245),
            _r(PSA, 3.1),
        ],
    ),
    (
        "2026-08-12",
        "2026-08-13",
        "papa",
        [
            _r(_HB_M, 13.1),
            _r(PLT, 205),
            _r(GLU, 99),
            _r(CREA_M, 1.4),
            _r(BUN, 26),
            _r(VITD, 31),
            _r(B12, 310),
            _r(PSA, 3.4),
        ],
    ),
    (
        "2026-10-07",
        "2026-10-08",
        "papa",
        [
            _r(_HB_M, 13.6),
            _r(CREA_M, 1.3),
            _r(BUN, 22),
            _r(NA, 136),
            _r(K, 4.6),
            _r(VITD, 38),
        ],
    ),
]
