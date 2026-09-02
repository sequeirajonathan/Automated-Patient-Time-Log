"""The Mobile Caregiver+ sign-off page as the phone published it.

Bounds and wording read off the live device on 1 Sep 2026; the names are
placeholders, because this file and anything built from it are shared.
"""
APP = "com.tellus.evv.v2"


def el(rid, cls, b, txt="", enabled=True):
    return {"rid": rid, "cls": cls, "b": b, "txt": txt, "checked": False,
            "focused": False, "selected": False, "enabled": enabled,
            "has_text": bool(txt)}


def st(b, txt, cls="TextView"):
    return {"cls": cls, "b": b, "txt": txt}


def doc():
    return {
        "id": "signoff", "img": "", "size": [1080, 2340],
        "app": APP, "screen": "home", "activity": "dashboardactivity",
        "blocked": "", "notice": "", "landscape": False, "turn": False,
        "canvas": False, "covered": False, "full": True, "scrollable": False,
        "nav": {"at": "VisitSignOffFragment", "at_says": "Firmas de la visita",
                "trail": ["VisitSignOffFragment"], "depth": 1,
                "says": ["Firmas de la visita"], "rooted": False},
        "elements": [
            el("main_back_button", "LinearLayout", [20, 101, 156, 180]),
            el("action_edit_note", "LinearLayout", [884, 101, 1070, 181]),
            el("", "View", [20, 347, 1060, 405]),        # section header
            el("", "View", [1000, 347, 1060, 405]),      # collapse chevron
            el("", "View", [30, 778, 1050, 856]),        # the dropdown
            el("", "View", [30, 876, 1050, 946]),        # Capturar Firma (patient)
            el("", "View", [30, 1080, 485, 1140]),       # blind checkbox
            el("", "View", [30, 1150, 1050, 1220]),      # Capturar Firma (staff)
            el("", "View", [20, 2119, 1060, 2189], enabled=False),  # Complete
            el("action_tab_patient", "FrameLayout", [434, 2210, 645, 2280],
               "Beneficiarios"),
            el("action_tab_messages", "FrameLayout", [645, 2210, 856, 2280],
               "Mensajes, 370 elementos"),
        ],
        "statics": [
            st([20, 139, 138, 180], "Regresar"),
            st([385, 119, 696, 162], "Firmas de la visita"),
            st([884, 141, 1057, 181], "Agregar nota"),
            st([20, 201, 1060, 349],
               "La visita está programada para [UN PACIENTE] en martes, "
               "1 de septiembre de 2026 de 6:00 PM a 8:00 PM"),
            st([20, 362, 1000, 392], "UN PACIENTE"),
            st([13, 419, 310, 457], "Duración del servicio:"),
            st([310, 419, 444, 457], "02h : 01m"),
            st([13, 467, 1067, 505],
               "Estado del servicio, Completada . T1019  "
               "(Personal care ser per 15 min)"),
            st([30, 538, 1050, 649],
               "Los firmantes confirman que los servicios anteriores se "
               "prestaron en martes, septiembre 01, 2026 desde 6:00:05 p. m. "
               "a 8:01:58 p. m. GMT-04:00"),
            st([30, 675, 210, 715], "Beneficiario"),
            st([30, 735, 319, 773], "¿Quien está firmando?"),
            st([30, 778, 1050, 856], "¿Quien está firmando?"),
            st([50, 798, 980, 836], "Recipient"),
            st([30, 876, 1050, 946], "Capturar Firma"),
            st([30, 1026, 211, 1070], "Cuidador/a"),
            st([30, 1080, 485, 1140],
               "no marcado. Marcar si el cuidador es ciego"),
            st([30, 1150, 1050, 1220], "Capturar Firma"),
            st([20, 2119, 1060, 2189], "Complete la Visita"),
            st([223, 2210, 434, 2280], "Visitas"),
            st([282, 2235, 374, 2276], "Visitas"),
            st([465, 2239, 614, 2275], "Beneficiarios"),
            st([740, 2210, 791, 2233], "370"),
            st([695, 2239, 806, 2275], "Mensajes"),
        ],
    }


def menu():
    """The signer dropdown, open. Its own window — the whole tree is this."""
    return {
        "id": "signoff-menu", "img": "", "size": [1080, 2340],
        "app": APP, "screen": "home", "activity": "dashboardactivity",
        "blocked": "", "notice": "", "landscape": False, "turn": False,
        "canvas": False, "covered": False, "full": True, "scrollable": True,
        "elements": [
            el("", "View", [30, 866, 380, 926]),
            el("", "View", [30, 926, 380, 986]),
            el("", "View", [30, 986, 380, 1062]),
            el("", "View", [30, 1062, 380, 1122]),
            el("", "View", [30, 1122, 380, 1182]),
        ],
        "statics": [
            st([50, 877, 268, 915], "Family Member"),
            st([50, 937, 258, 975], "Legal Guardian"),
            st([50, 986, 360, 1062], "No Signature Gathered"),
            st([50, 1073, 183, 1111], "Recipient"),
            st([50, 1133, 266, 1171], "Representative"),
        ],
    }
