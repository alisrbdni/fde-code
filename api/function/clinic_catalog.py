"""Static Northside Family Clinic data (Appendix C.1–C.4).

Pure data plus tag helpers; all behaviour (triage, slots, booking) lives in
clinic.py. Curly apostrophes are normalised to ' before phrase matching, so
the lists below only use the straight form.
"""

CLINIC_NAME = "Northside Family Clinic"
AGENT_NAME = "Clara"
CLINIC_ADDRESS = "200 Harbor Avenue, Suite 3"
CLINIC_HOURS_TEXT = "Monday to Friday 8 AM to 6 PM, and Saturday 9 AM to 1 PM"

# Front-desk hours per weekday (0 = Monday). None = closed.
DESK_HOURS = {
    0: ("08:00", "18:00"),
    1: ("08:00", "18:00"),
    2: ("08:00", "18:00"),
    3: ("08:00", "18:00"),
    4: ("08:00", "18:00"),
    5: ("09:00", "13:00"),
    6: None,
}

FACTS = (
    "We accept most major insurance plans; please bring your insurance card "
    "and a photo ID.",
    "New patients should arrive 15 minutes early to fill in forms.",
    "Please give at least 24 hours notice to cancel or change an appointment.",
    "Free parking is behind the building; the entrance is step-free.",
    "Lab tests and vaccinations are done on site, no appointment needed for "
    "flu shots on weekdays.",
    "Prescription refill requests are handled by your doctor's team within "
    "two business days.",
)

# No slot may *start* inside lunch (12:00–12:59).
LUNCH_HOUR = 12

SPECIALTIES = {
    "family_medicine": {
        "label": "family medicine",
        "who": "a family doctor",
        "keywords": (
            "check up", "checkup", "check-up", "physical", "annual exam",
            "cold", "flu", "fever", "cough", "fatigue", "tired",
            "vaccination", "vaccine", "shot", "blood pressure check",
            "prescription", "refill", "diabetes", "thyroid", "weight",
            "general", "not feeling well", "sick", "infection", "urinary",
            "burning when i pee", "allergies", "sleep",
        ),
    },
    "cardiology": {
        "label": "cardiology",
        "who": "a heart specialist",
        "keywords": (
            "palpitations", "heart racing", "racing heart",
            "irregular heartbeat", "skipped beats", "heart flutter",
            "high blood pressure", "hypertension", "heart murmur",
            "cholesterol", "swollen ankles", "ankle swelling",
            "short of breath when walking", "heart",
        ),
    },
    "dermatology": {
        "label": "dermatology",
        "who": "a skin specialist",
        "keywords": (
            "rash", "acne", "mole", "eczema", "psoriasis", "itchy skin",
            "itchy", "skin", "hives", "hair loss", "wart", "spot on my",
            "sunburn", "dry skin", "nail fungus", "freckle",
        ),
    },
    "orthopedics": {
        "label": "orthopedics",
        "who": "a bone and joint specialist",
        "keywords": (
            "knee", "back pain", "lower back", "shoulder", "joint",
            "sprain", "sprained", "hip", "ankle", "wrist", "elbow",
            "sports injury", "twisted", "fracture", "broken", "neck pain",
            "arthritis", "tendon", "muscle pain", "pulled muscle",
        ),
    },
    "ent": {
        "label": "ear, nose and throat",
        "who": "an ear, nose and throat specialist",
        "keywords": (
            "ear", "earache", "hearing", "sinus", "sore throat", "tonsil",
            "nosebleed", "hoarse", "ringing in my ears", "tinnitus",
            "blocked nose", "congestion", "snoring", "throat",
        ),
    },
    "gastroenterology": {
        "label": "gastroenterology",
        "who": "a stomach and digestion specialist",
        "keywords": (
            "stomach", "abdominal", "belly", "heartburn", "reflux", "acid",
            "nausea", "diarrhea", "diarrhoea", "constipation", "bloating",
            "indigestion", "bowel", "vomiting", "gas",
        ),
    },
    "neurology": {
        "label": "neurology",
        "who": "a neurologist",
        "keywords": (
            "headache", "migraine", "numbness", "tingling",
            "pins and needles", "tremor", "memory", "dizziness", "dizzy",
            "vertigo", "seizure history", "nerve pain",
        ),
    },
    "pediatrics": {
        "label": "pediatrics",
        "who": "a children's doctor",
        "keywords": (
            "my child", "my son", "my daughter", "my baby", "my kid",
            "toddler", "infant", "newborn", "child", "kids", "teenager",
            "school physical",
        ),
    },
    "womens_health": {
        "label": "women's health",
        "who": "a women's health specialist",
        "keywords": (
            "pregnant", "pregnancy", "period", "menstrual", "pelvic",
            "contraception", "birth control", "menopause", "pap smear",
            "prenatal", "breast",
        ),
    },
    "behavioral_health": {
        "label": "behavioral health",
        "who": "a mental health clinician",
        "keywords": (
            "anxiety", "anxious", "depressed", "depression", "stress",
            "panic", "mood", "can't sleep", "insomnia", "grief", "therapy",
            "counseling", "counselling",
        ),
    },
}

# C.3: id · name · specialty · days (0 = Monday) · start–end · slot min · languages
CLINICIANS = (
    {"id": "fm-okafor", "name": "Dr. Maya Okafor", "specialty": "family_medicine",
     "days": (0, 1, 2, 3, 4), "start": "08:30", "end": "17:00", "slot_min": 30,
     "languages": ("English",)},
    {"id": "fm-carter", "name": "Dr. Ben Carter", "specialty": "family_medicine",
     "days": (1, 3, 5), "start": "09:00", "end": "13:00", "slot_min": 20,
     "languages": ("English", "Spanish")},
    {"id": "card-reyes", "name": "Dr. Daniel Reyes", "specialty": "cardiology",
     "days": (0, 2, 4), "start": "09:00", "end": "16:00", "slot_min": 30,
     "languages": ("English", "Spanish")},
    {"id": "derm-raman", "name": "Dr. Priya Raman", "specialty": "dermatology",
     "days": (1, 2, 3), "start": "10:00", "end": "17:00", "slot_min": 30,
     "languages": ("English", "Hindi")},
    {"id": "ortho-becker", "name": "Dr. Tom Becker", "specialty": "orthopedics",
     "days": (0, 1, 3), "start": "08:00", "end": "15:00", "slot_min": 30,
     "languages": ("English", "German")},
    {"id": "ent-novak", "name": "Dr. Lena Novak", "specialty": "ent",
     "days": (2, 4), "start": "09:00", "end": "17:00", "slot_min": 30,
     "languages": ("English",)},
    {"id": "gi-kim", "name": "Dr. Samuel Kim", "specialty": "gastroenterology",
     "days": (0, 3), "start": "09:00", "end": "16:00", "slot_min": 30,
     "languages": ("English", "Korean")},
    {"id": "neuro-liu", "name": "Dr. Grace Liu", "specialty": "neurology",
     "days": (1, 4), "start": "09:00", "end": "16:00", "slot_min": 45,
     "languages": ("English", "Mandarin")},
    {"id": "peds-haddad", "name": "Dr. Omar Haddad", "specialty": "pediatrics",
     "days": (0, 1, 2, 3, 4), "start": "08:30", "end": "16:30", "slot_min": 30,
     "languages": ("English", "Arabic")},
    {"id": "wh-weiss", "name": "Dr. Hannah Weiss", "specialty": "womens_health",
     "days": (0, 2, 4), "start": "09:00", "end": "17:00", "slot_min": 30,
     "languages": ("English",)},
    {"id": "bh-alvarez", "name": "Ruth Alvarez, LCSW", "specialty": "behavioral_health",
     "days": (1, 2, 3, 4), "start": "11:00", "end": "19:00", "slot_min": 50,
     "languages": ("English", "Spanish")},
)

EMERGENCY_PHRASES = (
    "chest pain", "chest pressure", "crushing chest", "tight chest",
    "pain in my chest", "heart attack", "can't breathe", "cannot breathe",
    "can not breathe", "trouble breathing", "difficulty breathing",
    "struggling to breathe", "severe shortness of breath", "choking",
    "lips turning blue", "turning blue", "face drooping",
    "face is drooping", "slurred speech", "slurring", "one side of my body",
    "sudden numbness", "sudden weakness", "can't move my arm", "stroke",
    "unconscious", "passed out", "fainted", "unresponsive",
    "won't wake up", "severe bleeding", "bleeding heavily",
    "won't stop bleeding", "bleeding a lot", "coughing up blood",
    "vomiting blood", "throat closing", "throat is closing",
    "tongue swelling", "swollen tongue", "anaphylaxis",
    "allergic reaction and swelling", "overdose", "overdosed",
    "took too many pills", "poisoned", "poisoning", "seizure right now",
    "having a seizure", "is seizing", "worst headache of my life",
    "sudden severe headache", "stiff neck and fever", "head injury",
    "hit my head", "car accident", "bone sticking out",
    "pregnant and bleeding", "severe burn",
)

CRISIS_PHRASES = (
    "suicide", "suicidal", "kill myself", "end my life", "want to die",
    "don't want to live", "self harm", "self-harm", "hurt myself",
    "hurting myself", "cutting myself",
)

URGENT_PHRASES = (
    "high fever", "fever for three days", "fever for 3 days",
    "fever over 103", "fever of 103", "fever of 104", "broken", "fracture",
    "deep cut", "needs stitches", "burn", "infection is spreading",
    "spreading redness", "severe pain", "really bad pain",
    "blood in my stool", "blood in stool", "blood in my urine",
    "vomiting for", "can't keep anything down", "dehydrated",
    "baby has a fever", "child has a fever", "eye injury",
    "something in my eye", "swollen and hot", "getting worse quickly",
    "shortness of breath", "short of breath",
)

WEEKDAYS = (
    ("monday", "mon"), ("tuesday", "tue"), ("wednesday", "wed"),
    ("thursday", "thu"), ("friday", "fri"), ("saturday", "sat"),
    ("sunday", "sun"),
)


def clinician(clinician_id: str):
    """Return the clinician dict for an id, or None."""
    for spec in CLINICIANS:
        if spec["id"] == clinician_id:
            return spec
    return None


def specialty_who(specialty_id: str) -> str:
    """The spoken 'who to see' phrase for a specialty id."""
    spec = SPECIALTIES.get(specialty_id) or {}
    return spec.get("who", "a family doctor")
