"""Skill + certification dictionaries and the matching helpers built on them."""
import re

# category -> canonical skill -> extra aliases (the canonical name is always an alias too)
SKILLS = {
    "Languages": {
        "Python": [], "Java": [], "JavaScript": ["js", "ecmascript"], "TypeScript": [],
        "C++": [], "C#": [], "C": [], "Go": ["golang"], "Rust": [], "Ruby": [], "PHP": [],
        "Swift": [], "Kotlin": [], "Scala": [], "R": [], "SQL": [], "Bash": ["shell scripting"],
        "MATLAB": [], "Dart": [], "Perl": [],
    },
    "Web & Frameworks": {
        "Django": [], "Flask": [], "FastAPI": [], "React": ["react.js", "reactjs"],
        "Angular": ["angularjs"], "Vue": ["vue.js", "vuejs"], "Node.js": ["node", "nodejs"],
        "Express": ["express.js"], "Next.js": ["nextjs"], "Spring Boot": ["spring"],
        ".NET": ["dotnet", "asp.net"], "Laravel": [], "Ruby on Rails": ["rails"],
        "HTML": ["html5"], "CSS": ["css3"], "Tailwind CSS": ["tailwind"], "Bootstrap": [],
        "REST APIs": ["rest", "restful", "rest api"], "GraphQL": [], "jQuery": [],
    },
    "Cloud & DevOps": {
        "AWS": ["amazon web services", "ec2", "s3", "lambda"], "Azure": ["microsoft azure"],
        "GCP": ["google cloud", "google cloud platform"], "Docker": [], "Kubernetes": ["k8s"],
        "Terraform": [], "Ansible": [], "Jenkins": [], "CI/CD": ["cicd", "ci cd", "continuous integration"],
        "GitHub Actions": [], "GitLab CI": [], "Linux": [], "Nginx": [], "Helm": [],
        "Prometheus": [], "Grafana": [], "Serverless": [],
    },
    "Data & AI": {
        "PostgreSQL": ["postgres"], "MySQL": [], "MongoDB": [], "Redis": [], "Elasticsearch": [],
        "SQLite": [], "Oracle": [], "Snowflake": [], "BigQuery": [], "Spark": ["pyspark", "apache spark"],
        "Kafka": ["apache kafka"], "Airflow": ["apache airflow"], "Hadoop": [],
        "Pandas": [], "NumPy": [], "scikit-learn": ["sklearn"], "TensorFlow": [], "PyTorch": [],
        "Machine Learning": ["ml"], "Deep Learning": [], "NLP": ["natural language processing"],
        "Data Analysis": ["data analytics"], "Data Engineering": [], "ETL": [],
        "Tableau": [], "Power BI": ["powerbi"], "Excel": ["microsoft excel", "advanced excel"], "LLM": ["large language models"],
        "Matplotlib": [], "Seaborn": [], "Plotly": [], "Streamlit": [], "XGBoost": [], "spaCy": [],
        "Jupyter": ["jupyter notebook"], "Predictive Modeling": ["predictive modelling", "predictive analytics"],
        "Statistical Analysis": ["statistics", "statistical modeling"], "Data Visualization": ["data visualisation"],
        "TF-IDF": [], "SPSS": [], "Google Analytics": [], "Data Cleaning": ["data preprocessing"],
    },
    "Finance & Accounting": {
        "Financial Modeling": ["financial modelling"], "Financial Analysis": ["investment analysis"],
        "Budgeting": ["budgeting and forecasting"], "Forecasting": [], "Internal Audit": ["internal audit", "internal controls"],
        "Accounting": ["bookkeeping"], "Bank Reconciliation": ["reconciliation", "reconciliations", "bank reconciliations"],
        "Tally": ["tally erp", "tally prime"], "Accounts Payable": [], "Accounts Receivable": [],
        "Financial Reporting": ["month-end reporting"], "Risk Assessment": ["risk assessments"], "GST": [], "Taxation": [],
        "Market Research": [], "Business Analytics": ["business analysis", "business performance analysis"],
    },
    "Engineering Practices": {
        "Git": ["github", "gitlab", "bitbucket"], "Agile": ["scrum", "kanban"], "Microservices": [],
        "System Design": [], "Unit Testing": ["pytest", "junit", "jest", "tdd"], "Selenium": [],
        "Automation Testing": ["test automation"], "API Design": [], "Design Patterns": [],
        "Object-Oriented Programming": ["oop", "object oriented"], "Debugging": [],
        "Code Review": [], "Security": ["cybersecurity", "application security"],
    },
    "Business & Soft Skills": {
        "Project Management": [], "Product Management": [], "Leadership": ["team leadership"],
        "Stakeholder Management": [], "Communication": [], "Problem Solving": [],
        "Team Management": [], "Mentoring": [], "Jira": [], "Confluence": [],
        "Figma": [], "UI/UX": ["ux", "ui design", "user experience"], "SEO": [],
        "Salesforce": [], "SAP": [], "Recruitment": [], "Customer Support": [],
    },
}

# canonical cert -> (regex, provider, how to get it)
CERTS = {
    "AWS Certified Solutions Architect": (r"aws\s+(certified\s+)?solutions?\s+architect", "AWS", "https://aws.amazon.com/certification/"),
    "AWS Certified Developer": (r"aws\s+(certified\s+)?developer", "AWS", "https://aws.amazon.com/certification/"),
    "AWS Certified Cloud Practitioner": (r"aws\s+(certified\s+)?cloud\s+practitioner", "AWS", "https://aws.amazon.com/certification/"),
    "AWS Certification": (r"aws\s+certif\w*", "AWS", "https://aws.amazon.com/certification/"),
    "Microsoft Azure Certification": (r"(microsoft\s+)?azure\s+(certif\w*|az-\d+|fundamentals|administrator|developer)|\baz-\d{3}\b", "Microsoft", "https://learn.microsoft.com/credentials/"),
    "Google Cloud Certification": (r"(google\s+cloud|gcp)\s+(certif\w*|professional|associate)", "Google Cloud", "https://cloud.google.com/learn/certification"),
    "Certified Kubernetes Administrator (CKA)": (r"\bcka\b|certified\s+kubernetes\s+administrator", "CNCF", "https://training.linuxfoundation.org/certification/"),
    "Certified Kubernetes Application Developer (CKAD)": (r"\bckad\b", "CNCF", "https://training.linuxfoundation.org/certification/"),
    "HashiCorp Terraform Associate": (r"terraform\s+associate", "HashiCorp", "https://www.hashicorp.com/certification/"),
    "PMP": (r"\bpmp\b|project\s+management\s+professional", "PMI", "https://www.pmi.org/certifications"),
    "PRINCE2": (r"prince2", "Axelos", "https://www.axelos.com/"),
    "Certified ScrumMaster (CSM)": (r"\bcsm\b|certified\s+scrum\s*master|\bpsm\b", "Scrum Alliance / Scrum.org", "https://www.scrumalliance.org/"),
    "CISSP": (r"\bcissp\b", "ISC2", "https://www.isc2.org/certifications/cissp"),
    "CompTIA Security+": (r"security\+|comptia\s+security", "CompTIA", "https://www.comptia.org/certifications"),
    "CEH": (r"\bceh\b|certified\s+ethical\s+hacker", "EC-Council", "https://www.eccouncil.org/"),
    "CCNA": (r"\bccna\b", "Cisco", "https://www.cisco.com/c/en/us/training-events/training-certifications/certifications.html"),
    "Oracle Certification": (r"oracle\s+certified|\bocp\b", "Oracle", "https://education.oracle.com/"),
    "Salesforce Certification": (r"salesforce\s+certified", "Salesforce", "https://trailhead.salesforce.com/credentials"),
    "Six Sigma": (r"six\s+sigma|lean\s+six", "ASQ / IASSC", "https://asq.org/cert"),
    "ITIL": (r"\bitil\b", "Axelos", "https://www.axelos.com/"),
    "CPA": (r"\bcpa\b|certified\s+public\s+accountant", "AICPA", "https://www.aicpa-cima.com/"),
    "CFA": (r"\bcfa\b|chartered\s+financial\s+analyst", "CFA Institute", "https://www.cfainstitute.org/"),
    "Databricks Certification": (r"databricks\s+certified", "Databricks", "https://www.databricks.com/learn/certification"),
    "Tableau Certification": (r"tableau\s+(desktop\s+)?(certified|specialist)", "Tableau", "https://www.tableau.com/learn/certification"),
    "TensorFlow Developer Certificate": (r"tensorflow\s+developer\s+certificate", "Google", "https://www.tensorflow.org/certificate"),
    "ISTQB": (r"istqb", "ISTQB", "https://www.istqb.org/"),
}

_NEG = r"(?<![A-Za-z0-9+#.])"
_NEG_AFTER = r"(?![A-Za-z0-9+#]|\.[A-Za-z0-9])"


def _alias_pattern(alias):
    return _NEG + re.escape(alias).replace(r"\ ", r"[\s\-]+") + _NEG_AFTER


# Pre-compile one regex per canonical skill
_SKILL_RX = []
for _cat, _group in SKILLS.items():
    for _name, _aliases in _group.items():
        _alts = "|".join(_alias_pattern(a) for a in {_name, *_aliases})
        _SKILL_RX.append((_cat, _name, re.compile(_alts, re.I)))

# Short ambiguous names are only accepted in their exact, usual casing
_CASE_SENSITIVE = {"C", "R", "Go", "Node.js", "Swift", "Oracle", "Express", "Spring Boot", "SAP", "Security", "Excel"}
_CASE_SENSITIVE_RX = {
    n: re.compile(_NEG + re.escape(n) + _NEG_AFTER) for n in _CASE_SENSITIVE
}
_CASE_SENSITIVE_RX["Go"] = re.compile(_NEG + r"(Go|golang|Golang)" + _NEG_AFTER)
_CASE_SENSITIVE_RX["Security"] = re.compile(r"\b(?:Security|security|cybersecurity|application security)\b")
_CASE_SENSITIVE_RX["Excel"] = re.compile(r"\b(?:Excel|excel|Microsoft Excel)\b")

_CERT_RX = {n: re.compile(rx, re.I) for n, (rx, _p, _u) in CERTS.items()}


def find_skills(text):
    """Return {category: [skills]} for every dictionary skill found in text."""
    found = {}
    for cat, name, rx in _SKILL_RX:
        hit = _CASE_SENSITIVE_RX[name].search(text) if name in _CASE_SENSITIVE else rx.search(text)
        if hit:
            found.setdefault(cat, []).append(name)
    return found


def flat_skills(found):
    return [s for group in found.values() for s in group]


def skill_category(skill):
    for cat, group in SKILLS.items():
        if skill in group:
            return cat
    return "Other"


def find_certs(text):
    """Return canonical certification names found in text. Generic names are dropped when a specific one matched."""
    hits = [n for n, rx in _CERT_RX.items() if rx.search(text)]
    if any(h.startswith("AWS Certified") for h in hits) and "AWS Certification" in hits:
        hits.remove("AWS Certification")
    return hits


def cert_info(name):
    _rx, provider, url = CERTS[name]
    return {"name": name, "provider": provider, "url": url}
