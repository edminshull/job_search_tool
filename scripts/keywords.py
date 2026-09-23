"""Keyword extraction and CV coverage analysis.

Two jobs:

1. Pull the terms an ATS or a human screener would key on out of a job post,
   and weight them by where they appear (requirements > duties > nice-to-have).
2. Check which of those terms your CV can actually evidence.

Precision matters more than recall here. A report that lists "advanced",
"comparable" and "solid" as skill gaps is worse than no report, because it
buries the two lines that actually matter. So:

  - function words are dropped (STOPWORDS)
  - job-post filler is dropped (BOILERPLATE), unless it is part of a recognised
    multi-word term, which is why "code review" and "window functions" survive
    while bare "code" and "window" do not
  - terms are matched on a light stem, so "pipelines" matches "pipeline" and
    "warehousing" matches "warehouse"
  - known skills are tagged, so the report can separate hard gaps from prose

Nothing here invents experience. It only reports overlap, so 'missing' means
"the posting uses this word and your CV never does" -- which may be a real gap
or merely a phrasing problem.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Lexicon: surface form -> canonical term
# --------------------------------------------------------------------------

SYNONYMS: dict[str, str] = {
    # --- data / engineering -------------------------------------------------
    "postgres": "postgresql", "psql": "postgresql",
    "js": "javascript", "ts": "typescript",
    "k8s": "kubernetes",
    "node": "node.js", "nodejs": "node.js",
    "reactjs": "react", "react.js": "react",
    "ml": "machine learning", "ai": "artificial intelligence",
    "genai": "generative ai",
    "llm": "large language models", "llms": "large language models",
    "nlp": "natural language processing",
    "ci": "ci/cd", "cd": "ci/cd", "ci cd": "ci/cd", "cicd": "ci/cd",
    "continuous integration": "ci/cd",
    "continuous delivery": "ci/cd", "continuous deployment": "ci/cd",
    "gcp": "google cloud platform", "aws": "amazon web services",
    "etl": "etl", "elt": "etl",
    "etl pipelines": "etl", "data pipelines": "data pipelines",
    "restful": "rest", "rest api": "rest apis", "restful apis": "rest apis",
    "microservice": "microservices", "micro services": "microservices",
    "data warehouse": "data warehousing", "dwh": "data warehousing",
    "warehouse": "data warehousing", "warehousing": "data warehousing",
    "dimensional modelling": "dimensional modelling",
    "dimensional modeling": "dimensional modelling",
    "star schema": "dimensional modelling",
    "query tuning": "query optimisation",
    "query optimisation": "query optimisation", "query optimization": "query optimisation",
    "query performance": "query optimisation",
    "performance tuning": "performance optimisation",
    "window functions": "window functions", "window function": "window functions",
    "code review": "code review", "code reviews": "code review",
    "peer review": "code review",
    "data quality": "data quality", "data quality frameworks": "data quality",
    "data contracts": "data contracts", "data contract": "data contracts",
    "data platform": "data platform", "data platforms": "data platform",
    "data modelling": "data modelling", "data modeling": "data modelling",
    "feature store": "feature store", "feature stores": "feature store",
    "stream processing": "stream processing", "streaming": "stream processing",
    "batch processing": "batch processing", "batch pipelines": "batch processing",
    "event driven": "event-driven architecture",
    "event driven architecture": "event-driven architecture",
    "message queue": "message queues", "message queues": "message queues",
    "distributed systems": "distributed systems",
    "system design": "system design", "systems design": "system design",
    "infrastructure as code": "infrastructure as code",
    "observability": "observability", "monitoring": "monitoring",
    "incident response": "incident response",
    "site reliability engineering": "site reliability engineering",
    "sre": "site reliability engineering",
    "on call": "on-call rotation", "on-call": "on-call rotation",
    "unit testing": "testing", "integration testing": "testing",
    "test driven development": "test-driven development",
    "tdd": "test-driven development",
    "version control": "git", "git": "git",
    "oauth": "oauth", "sso": "single sign-on",
    # --- tools --------------------------------------------------------------
    "powerbi": "power bi", "power bi": "power bi",
    "scikit learn": "scikit-learn", "sklearn": "scikit-learn",
    "airflow": "apache airflow", "apache airflow": "apache airflow",
    "spark": "apache spark", "pyspark": "apache spark", "apache spark": "apache spark",
    "kafka": "apache kafka", "apache kafka": "apache kafka",
    "kinesis": "amazon kinesis", "amazon kinesis": "amazon kinesis",
    "aws glue": "aws glue", "glue": "aws glue",
    "s3": "amazon s3", "amazon s3": "amazon s3",
    "iam": "aws iam", "aws iam": "aws iam",
    "ec2": "amazon ec2", "lambda": "aws lambda",
    "github actions": "github actions", "gitlab ci": "gitlab ci",
    "docker": "docker", "kubernetes": "kubernetes", "terraform": "terraform",
    "jenkins": "jenkins", "ansible": "ansible",
    "snowflake": "snowflake", "bigquery": "bigquery", "redshift": "redshift",
    "databricks": "databricks", "synapse": "azure synapse",
    "azure": "microsoft azure", "microsoft azure": "microsoft azure",
    "dbt": "dbt", "looker": "looker", "tableau": "tableau",
    "superset": "apache superset", "metabase": "metabase",
    "excel vba": "excel vba", "vba": "excel vba",
    "salesforce": "salesforce", "hubspot": "hubspot",
    "netsuite": "netsuite", "quickbooks": "quickbooks",
    "servicenow": "servicenow", "jira": "jira", "confluence": "confluence",
    "figma": "figma", "sagemaker": "amazon sagemaker",
    # --- languages ----------------------------------------------------------
    "golang": "go", "go lang": "go",
    "dotnet": ".net", "node.js": "node.js",
    "ruby on rails": "ruby on rails", "rails": "ruby on rails",
    "spring boot": "spring boot",
    "objective c": "objective-c",
    # --- analytics / ml -----------------------------------------------------
    "statistical analysis": "statistics",
    # 'regression' alone is ambiguous. In a testing posting it means regression
    # testing, not regression analysis, so only explicit forms are mapped.
    "regression analysis": "regression analysis",
    "regression testing": "regression testing",
    "regression tests": "regression testing",
    "regression suite": "regression testing",
    "regression pack": "regression pack",
    "regression packs": "regression pack",
    "regression pack maintenance": "regression pack",
    "predictive modelling": "predictive modeling",
    "predictive modeling": "predictive modeling",
    "a/b testing": "a/b testing", "ab testing": "a/b testing", "a/b": "a/b testing",
    "ab tests": "a/b testing", "split testing": "a/b testing",
    "data science": "data science",
    "data engineering": "data engineering",
    "data analysis": "data analysis", "data analytics": "data analytics",
    "data visualisation": "data visualization", "data visualization": "data visualization",
    "business intelligence": "business intelligence", "bi": "business intelligence",
    "forecasting": "forecasting", "time series": "time series analysis",
    "computer vision": "computer vision", "deep learning": "deep learning",
    "mlops": "mlops", "feature engineering": "feature engineering",
    # --- business / commercial ---------------------------------------------
    "kpi": "kpis", "okr": "okrs",
    "p&l": "p&l", "p and l": "p&l", "profit and loss": "p&l",
    "roi": "return on investment", "return on investment": "return on investment",
    "financial modeling": "financial modeling", "financial modelling": "financial modeling",
    "due diligence": "due diligence",
    "m&a": "mergers and acquisitions", "m and a": "mergers and acquisitions",
    "mergers and acquisitions": "mergers and acquisitions",
    "go to market": "go-to-market", "go-to-market": "go-to-market", "gtm": "go-to-market",
    "total addressable market": "total addressable market",
    "budget management": "budget management", "budgeting": "budgeting",
    "cost optimisation": "cost optimisation", "cost optimization": "cost optimisation",
    "process improvement": "process improvement",
    "continuous improvement": "continuous improvement",
    "change management": "change management",
    "risk management": "risk management",
    "vendor management": "vendor management",
    "contract negotiation": "contract negotiation",
    "supply chain": "supply chain", "procurement": "procurement",
    "inventory management": "inventory management", "logistics": "logistics",
    "stakeholder management": "stakeholder management",
    "stakeholder engagement": "stakeholder management",
    "cross functional": "cross-functional collaboration",
    "cross-functional": "cross-functional collaboration",
    "commercial acumen": "commercial acumen",
    "business partnering": "business partnering",
    "strategic planning": "strategic planning",
    "project management": "project management",
    "programme management": "programme management",
    "product roadmap": "roadmapping", "roadmap": "roadmapping",
    "roadmapping": "roadmapping",
    "product management": "product management",
    "agile": "agile", "scrum": "scrum", "kanban": "kanban", "safe": "scaled agile",
    "lean six sigma": "lean six sigma", "six sigma": "lean six sigma",
    # --- marketing / sales / cs --------------------------------------------
    "content marketing": "content marketing", "email marketing": "email marketing",
    "paid social": "paid social", "paid search": "paid search",
    "google analytics": "google analytics", "ga4": "google analytics",
    "marketing automation": "marketing automation",
    "brand strategy": "brand strategy", "copywriting": "copywriting",
    "social media": "social media", "public relations": "public relations",
    "lead generation": "lead generation", "pipeline management": "pipeline management",
    "quota": "quota attainment", "upselling": "upselling", "cross-selling": "cross-selling",
    "renewals": "renewals", "churn": "churn reduction",
    "customer success": "customer success", "account management": "account management",
    "nps": "nps", "csat": "csat",
    # --- people / ops ------------------------------------------------------
    "mentoring": "mentoring", "coaching": "coaching",
    "line management": "line management", "people management": "people management",
    "team leadership": "team leadership",
    "performance management": "performance management",
    "resource planning": "resource planning",
    "escalation management": "escalation management",
    "slas": "slas", "sla": "slas",
    "documentation": "documentation", "technical writing": "technical writing",
    "written communication": "written communication",
    "verbal communication": "verbal communication",
    "communication skills": "communication",
    # --- software testing / QA ---------------------------------------------
    # Bare 'test', 'testing' and 'automation' are stopped as filler; the
    # meaningful senses live in these phrases, which also suppress their own
    # constituent tokens.
    "test automation": "test automation",
    "automated testing": "test automation",
    "test automation framework": "test automation",
    "test framework": "test automation",
    "page object model": "page object model",
    "page objects": "page object model",
    "data driven testing": "data-driven testing",
    "data-driven testing": "data-driven testing",
    "data driven": "data-driven testing",
    "keyword driven testing": "keyword-driven testing",
    "keyword-driven testing": "keyword-driven testing",
    "reusable components": "reusable components",
    "reusable libraries": "reusable components",
    "reporting libraries": "reporting libraries",
    "reporting dashboards": "reporting dashboards",
    "reporting dashboard": "reporting dashboards",
    "test data": "test data",
    "test data handling": "test data",
    "test data management": "test data",
    "backend validation": "backend validation",
    "exception handling": "exception handling",
    "script maintenance": "script maintenance",
    "branching strategy": "branching strategy",
    "coding standards": "coding standards",
    "peer review": "code review",
    "version control": "git",
    "smoke testing": "smoke testing",
    "smoke tests": "smoke testing",
    "sanity testing": "sanity testing",
    "end to end testing": "end-to-end testing",
    "end-to-end testing": "end-to-end testing",
    "integration testing": "integration testing",
    "integration tests": "integration testing",
    "component testing": "component testing",
    "contract testing": "contract testing",
    "consumer driven contract testing": "contract testing",
    "performance testing": "performance testing",
    "load testing": "performance testing",
    "api testing": "api testing",
    "api automation": "api testing",
    "rest apis": "rest apis", "rest api": "rest apis",
    "ui testing": "ui testing", "ui automation": "ui testing",
    "web automation": "ui testing",
    "manual testing": "manual testing",
    "manual test cases": "manual testing",
    "exploratory testing": "exploratory testing",
    "functional testing": "functional testing",
    "defect management": "defect management",
    "test management": "test management",
    "test strategy": "test strategy",
    "test plan": "test strategy",
    "test cases": "test cases", "test case": "test cases",
    "business flows": "business flows", "business flow": "business flows",
    "automation feasibility": "automation feasibility",
    "test execution": "test execution",
    "test suites": "test suites", "test suite": "test suites",
    "shift left testing": "shift-left testing",
    "shift-left testing": "shift-left testing",
    "test coverage": "test coverage",
    "runbooks": "runbooks", "runbook": "runbooks",
    "handover": "handover",
    "test reporting": "test reporting",
    "quality engineering": "quality engineering",
"quality assurance": "quality assurance",
    "qa": "quality assurance",
    "defect triage": "defect triage",
    "root cause analysis": "root cause analysis",
    # Local LLM stack, ED-CONFIRMED 2026-09-22 and PERSONAL-BASIS. Self-keys added
    # because a named tool needs one to be CLASSIFIED as a hard skill in a report —
    # without it, an advert naming Ollama extracts it as an unknown capitalised
    # term and it lands in "other phrasing" instead of the hard-skill table, which
    # understates the gap for anyone else reading the report. `llama.cpp` and
    # `qwen3-coder` carry a dot and a hyphen respectively, so both spellings are
    # mapped; the spaced form is not always what an advert writes.
    # ED-CONFIRMED 2026-09-22, asked because the Cognizant advert makes OOP a
    # Required Skill. Neither had a key, so both extracted as unknown capitalised
    # terms and could never be classified as hard skills.
    "object-oriented programming": "object-oriented programming",
    "object oriented programming": "object-oriented programming",
    "object-oriented": "object-oriented programming",
    "object oriented": "object-oriented programming",
    "oop": "object-oriented programming",
    "data validation": "data validation",
    # ED-CONFIRMED 2026-09-22, asked because the Experis advert lists "JIRA and
    # Confluence" as a requirement and Confluence appeared nowhere in the master.
    "confluence": "confluence",
    "ollama": "ollama",
    "llama.cpp": "llama.cpp",
    "llama cpp": "llama.cpp",
    "qwen": "qwen",
    "qwen3-coder": "qwen3-coder",
    "qwen3 coder": "qwen3-coder",
    # Aliases added 2026-09-22. The spaced form was the ONLY key, and the
    # Elsevier advert writes it HYPHENATED ("strong analytical and root-cause
    # analysis skills"), which normalise() preserves — so the phrase never matched
    # and the advert's requirement fragmented into `root-cause` + `analysis`,
    # neither of which is a skill term. A self-key alone is not enough for a phrase
    # an advert may hyphenate; this is the documented "add the self-mapping as well
    # as any aliases" step. `root cause` (and its plural, via the final-word plural
    # tolerance) is included because postings ask to "identify root causes" without
    # ever using the word "analysis", and that is the same capability.
    "root-cause analysis": "root cause analysis",
    "root cause": "root cause analysis",
    "root-cause": "root cause analysis",
    # --- banking domain -----------------------------------------------------
    "core banking": "core banking",
    "private banking": "private banking",
    "wealth management": "wealth management",
    "digital banking": "digital banking",
    "retail banking": "retail banking",
    "investment banking": "investment banking",
    "open banking": "open banking",
    "temenos": "temenos",
    "kyc": "kyc", "know your customer": "kyc",
    "aml": "aml", "anti money laundering": "aml",
    "sanctions screening": "sanctions screening",
    "transaction monitoring": "transaction monitoring",
    "payment processing": "payments", "payments": "payments",
    "lending": "lending", "mortgages": "mortgages",
    "swift": "swift messaging", "iso 20022": "iso 20022",
    "sepa": "sepa", "faster payments": "faster payments",
    "psd2": "psd2", "open banking apis": "open banking",
    # --- governance --------------------------------------------------------
    "gdpr": "gdpr", "hipaa": "hipaa", "soc 2": "soc 2", "iso 27001": "iso 27001",
    "sarbanes-oxley": "sarbanes-oxley", "sox": "sarbanes-oxley",
    "ifrs": "ifrs", "gaap": "gaap", "audit": "audit",
    "compliance": "compliance", "data governance": "data governance",
    "security clearance": "security clearance",
    "itil": "itil", "accessibility": "accessibility", "wcag": "accessibility",
    # --- canonical self-mappings -------------------------------------------
    # A term is only classified as a *hard skill* in the report when its surface
    # form is a lexicon key. Every canonical worth classifying therefore needs
    # to appear as its own key. Without this block, 'Python', 'Selenium',
    # 'Cucumber', 'JUnit' and 'SQL' were extracted as anonymous tokens and
    # misfiled under "methods and other phrasing" instead of "hard skills" —
    # which understated every genuine tooling gap.
    "python": "python", "javascript": "javascript", "typescript": "typescript",
    "ruby": "ruby", "groovy": "groovy", "java": "java", "scala": "scala",
    "kotlin": "kotlin", "swift": "swift", "rust": "rust", "php": "php",
    "perl": "perl", "matlab": "matlab", "sql": "sql", "plsql": "sql",
    "t-sql": "sql", "nosql": "nosql",
    "selenium": "selenium", "selenium webdriver": "selenium webdriver",
    "webdriver": "selenium webdriver", "cypress": "cypress",
    "playwright": "playwright", "appium": "appium", "postman": "postman",
    "soap ui": "soap ui", "soapui": "soap ui",
    "junit": "junit", "testng": "testng", "cucumber": "cucumber",
    "specflow": "specflow", "robot framework": "robot framework",
    "jmeter": "jmeter", "gatling": "gatling", "k6": "k6", "locust": "locust",
    "circleci": "circleci", "travis ci": "travis ci", "bamboo": "bamboo",
    "azure devops": "azure devops", "teamcity": "teamcity",
    "mysql": "mysql", "h2": "h2", "oracle": "oracle",
    "sql server": "sql server", "sqlite": "sqlite",
    "elasticsearch": "elasticsearch",
    "testrail": "testrail", "zephyr": "zephyr", "qtest": "qtest",
    "datadog": "datadog", "splunk": "splunk", "grafana": "grafana",
    "prometheus": "prometheus", "kibana": "kibana",
    "maven": "maven", "gradle": "gradle", "ant": "apache ant",
    # BDD / collaboration practices and cross-language API tooling
    "3 amigos": "three amigos", "example mapping": "example mapping",
    "test first": "test-driven development", "test-first": "test-driven development",
    # The bare adjective, with no "development" after it. Adverts write both
    # "Apply Test-Driven Development (TDD)" and "work in a test-driven and Agile
    # fashion", and only the first form was a key — so the second extracted as a
    # term of its own, and once compounds started requiring adjacency it appeared
    # as a GAP sitting in the same report a few rows below the evidenced
    # "test-driven development" it is a synonym of. Two spellings of one practice
    # must resolve to one canonical, the same reason `restassured` needed a
    # spaced alias.
    "test-driven": "test-driven development", "test driven": "test-driven development",
    "sdk": "sdk", "sdks": "sdk",
    "testcafe": "testcafe", "test cafe": "testcafe",
    "readyapi": "readyapi", "ready api": "readyapi",
    "waterfall": "waterfall",
    # "Rest Assured" is how THREE real postings write it — with a space. The
    # extractor tokenises that into 'rest' + 'assured', and neither token is a
    # lexicon key, so the compound term never forms and the requirement is
    # silently LOST. It surfaced as a priority gap called "assured", which reads
    # like a real miss and is actually the engine failing to see Ed's own tool
    # named first in the advert's API requirement. Self-key plus spaced and
    # hyphenated aliases.
    "restassured": "restassured", "rest assured": "restassured",
    "rest-assured": "restassured",
    # Named as a required AI tool in the financial-data posting and extracted as
    # NOTHING AT ALL before this — a required tool invisible to the report.
    "n8n": "n8n",
    # Data formats a posting can require by name. Absent from the master, so
    # these report as gaps; that is correct and cheaply fixable by rewording.
    "json": "json", "xml": "xml", "soap": "soap ui", "soapui": "soap ui",
    "soap ui": "soap ui",
    # Named tools from the Harnham advert cluster. Each appeared in a real
    # posting and, without a self-key, was either misfiled as generic phrasing
    # (selenide landed under "other phrasing" rather than hard skills) or sank
    # into the truncated background list as an anonymous capitalised token.
    "selenide": "selenide",
    "github actions": "github actions", "gh actions": "github actions",
    "elasticsearch": "elasticsearch", "elastic search": "elasticsearch",
    "sentry": "sentry",
    "jmeter": "jmeter", "apache jmeter": "jmeter",
    "gatling": "gatling",
    # API-description tooling. Named outright in advert 5 ("tools such as Rest
    # Assured, Postman, Swagger") and, without a self-key, extracted as an
    # anonymous capitalised token — the same invisibility the Rest Assured bug
    # had. A named tool a posting asks for must be able to surface as a gap.
    "swagger": "swagger", "openapi": "openapi", "open api": "openapi",
    "swagger ui": "swagger",
    # Test-leadership and non-functional practice names. "NON-FUNCTIONAL TESTING"
    # is the important one: it CONTAINS "functional testing", so before this key
    # the matcher credited Ed with a discipline he has never worked in — a false
    # POSITIVE on a real gap, which is worse than a miss. The rest were invisible
    # (bare "gates", "resilience", "operational", "readiness" tokens).
    "non-functional testing": "non-functional testing",
    "non functional testing": "non-functional testing",
    "nfr testing": "non-functional testing",
    "quality gates": "quality gates", "quality gate": "quality gates",
    "security testing": "security testing", "penetration testing": "security testing",
    "pen test": "security testing", "penetration test": "security testing",
    "resilience testing": "resilience testing", "recovery testing": "resilience testing",
    "operational readiness": "operational readiness",
    "continuous testing": "continuous testing",
    "release acceptance testing": "release acceptance testing",
    "volume testing": "volume testing",
    "rdbms": "rdbms", "relational database": "rdbms",
    "relational databases": "rdbms",
    # Ed confirmed DevOps capability on 2026-09-21 and it became a master skill
    # item, which exposed the gap: `devops` had NO self-key, so a posting writing
    # it in lowercase outside a requirements heading sank into the background
    # list instead of the hard-skill tables. It surfaced on the Lead QA advert
    # only by luck — the advert capitalises "DevOps" in its Technologies tag
    # list, which the capitalisation signal picked up. Same root cause as the
    # Rest Assured bug: a named technology has to be able to surface as a skill.
    "devops": "devops", "dev ops": "devops",
    # Named in the Java & Azure SDET advert and invisible before: "GitLab" was an
    # anonymous capitalised token, and "F2B" was not extracted at all.
    "gitlab": "gitlab", "gitlab ci": "gitlab", "gitlab ci/cd": "gitlab",
    "git lab": "gitlab",
    "f2b": "front-to-back testing", "front-to-back": "front-to-back testing",
    "front to back": "front-to-back testing",
    "front-to-back testing": "front-to-back testing",
    "cloud-native": "cloud-native", "cloud native": "cloud-native",
    # Named in the Xe advert. "pact" had NO self-key, so Ed's exact hit on a
    # nice-to-have ("contract testing for partner integrations (Pact or
    # equivalent)") was classified as an anonymous capitalised token rather than
    # a hard skill. The rest were invisible entirely.
    "pact": "pact",
    "bruno": "bruno",
    "cursor": "cursor", "cursor ai": "cursor",
    "agentic": "agentic coding", "agentic coding": "agentic coding",
    "agentic coding tools": "agentic coding",
    "eval framework": "eval frameworks", "eval frameworks": "eval frameworks",
    "evaluation framework": "eval frameworks",
    "release gate": "release gate", "release gates": "release gate",
    "windows": "windows", "windows client": "windows",
    # Domain terms at the centre of the Xe role and absent from the lexicon.
    "fx": "fx", "foreign exchange": "fx",
    "kyb": "kyb",
    "settlement": "settlement", "settlements": "settlement",
    "root cause analysis": "root cause analysis",
    "test coverage": "test coverage",
    # Test-stack layers and reliability practices. These are named practices a
    # posting asks for by name, but they are ordinary lowercase words, so without
    # a self-key they were extracted as anonymous tokens and sank into the
    # background list — the Adaptive advert asks for unit / component /
    # integration / system / E2E coverage and profiling and chaos testing, and
    # the report showed none of them as gaps because of that. An absence the
    # report cannot see is worse than no report.
    "unit testing": "unit testing", "unit tests": "unit testing", "unit test": "unit testing",
    "profiling": "application profiling", "application profiling": "application profiling",
    "profilers": "application profiling", "memory profiling": "application profiling",
    "chaos testing": "chaos testing", "chaos engineering": "chaos testing",
    "fault injection": "chaos testing",
    "static code analysis": "static code analysis", "static analysis": "static code analysis",
    "code analysis": "static code analysis",
    "telemetry": "telemetry",
    "system integration testing": "system integration testing",
    "sit": "system integration testing",
    "test pyramid": "test pyramid", "testing pyramid": "test pyramid",
    "risk-based testing": "risk-based testing", "risk based testing": "risk-based testing",
    "jest": "jest", "supertest": "supertest",
    "restsharp": "restsharp", "httpclient": "httpclient",
    # security and accessibility tooling
    "owasp": "owasp", "snyk": "snyk", "grype": "grype",
    "uipath": "uipath", "copilot": "github copilot",
    # AI phrasing as postings write it
    "ai-assisted": "ai-assisted development",
    "ai-assisted testing": "ai-assisted test generation",
    "ai-assisted test generation": "ai-assisted test generation",
    "ai-powered": "artificial intelligence",
    "ai-enabled": "artificial intelligence",
    "llm-assisted": "llm-assisted development",
    "prompt engineering": "prompt engineering",
    "local llm": "local llm deployment", "local llms": "local llm deployment",
    "self-hosted llm": "local llm deployment", "self hosted ai": "local llm deployment",
    "port forwarding": "port forwarding", "port-forwarding": "port forwarding",
    "port forward": "port forwarding", "kubectl port-forward": "port forwarding",
    "github copilot": "github copilot", "claude code": "claude code",
    "npm": "npm", "webpack": "webpack",
    "appdynamics": "appdynamics", "new relic": "new relic",
}

# --------------------------------------------------------------------------
# Word lists
# --------------------------------------------------------------------------

STOPWORDS: frozenset[str] = frozenset(
    """
    a about above across after again against all also am an and any are aren as at
    be because been before being below between both but by can cannot could couldn
    did didn do does doesn doing don down during each either else etc even ever
    every few for from further had hadn has hasn have haven having he her here
    hers herself him himself his how however i if in into is isn it its itself
    just least less ll ma may me might mine more most much must my myself neither
    never no nor not now of off on once one only or other ought our ours ourselves
    out over own re s same shan she should shouldn so some such than that the
    their theirs them themselves then there these they this those through to too
    under until up upon us ve very was wasn we were weren what when where whether
    which while who whom why will with within without won would wouldn you your
    yours yourself yourselves
    """.split()
)

# Job-post filler. These are dropped as bare tokens but still count inside a
# recognised multi-word term (so "code review" survives, bare "code" does not).
BOILERPLATE: frozenset[str] = frozenset(
    """
    ability able about above across additional advantage advantageous advanced
    advertise advertising applicant applicants application applications apply
    background backgrounds bar base benefits best bonus build building builds
    built candidate candidates career code coding company comparable competitive
    culture day days degree demonstrable demonstrated depending desirable desired
    duties duty dynamic employee employees employer employment enjoy ensure
    environment equal excellent exciting expected experience experienced expert
    exposure favourite flexible following full function functions get given great
    group grow growth help high highly hire hiring hands-on ideal including
    inclusive job join joining key knowledge like location look looking love make
    making manage managing minimum mission month months must new offer office
    opportunity organisation organization ownership package paid part passion
    passionate people perform perks permanent person plan please plus
    position positive possible potential preferred previous private proactive
    process professional profile progress proven provide providing qualification
    qualifications query raising range rate record recruit recruiting recruitment
    reference relating relevant remote report reporting required requirement
    requirements requiring responsibilities responsibility responsible review
    reviews right role roles salary scale seek seeking similar skills solid
    someone staff stakeholder stakeholders standard standards strong successful
    suitably support supporting team teams technical thing things time times title
    tooling tools track training tuning typical understand understanding variety
    various want welcome well window work working world written year years
    """.split()
)

# Generic engineering prose. These are not skills — they are the connective
# tissue of every job posting ever written. Bare 'test', 'testing' and
# 'automation' live here too, because in an SDET posting they are the domain
# rather than a differentiator; the meaningful senses are captured by the
# phrases in SYNONYMS, which suppress their own constituent tokens.
BOILERPLATE: frozenset[str] = BOILERPLATE | frozenset(
    """
    across agreed analyse analyze approved automate automated common complexity
    component components create creates data driven design designs develop
    developing development end enterprise equivalent execute executes expertise
    feasible feasibility good handle handling identify keyword libraries
    maintainable maintain maintaining maintenance manage manages modular ongoing
    optimization optimizing optional outcome outcomes parameterization pattern
    patterns prepare prepares priority provides provide reusable reuse script
    scripts sprint sprint based suites suites test tested testing tests using
    use used uses utilities utility versus within
    """.split()
)

# Corporate-prose padding. Some postings are written in heavy HR register
# ("contribute to the ongoing development of...", "in accordance with agreed
# standards"), and without this the priority-gap list fills up with words like
# 'appropriate' instead of the two tools the employer actually named.
BOILERPLATE: frozenset[str] = BOILERPLATE | frozenset(
    """
    accordance accuracy activities activity adaptability advisers appropriate
    assessments awareness ceremonies checklists clearly colleagues contribute
    contributes curiosity customers dependent documented effectiveness effective
    efficiency efficient environment environments escalate escalation expectations
    goals improvement improvements improve initiative integrity interaction
    investigate investigation layers lifecycle maintainability maximise maximize
    measure measures methodologies methodology opportunities orientation outdated
    partnership practices procedures proactivity progress redundant refinement
    relevant reliability reliable removal resolution resources retrospectives
    reusability sets solely software structured technique techniques technologies
    technology thoroughness transaction transactions understanding unreliable
    validate validation
    attention detail take line inclusion diversity
    sound diagnose uphold brand extensive managers promote project
    apply applies assess communicate convert participate retest share tester testers
    familiarity proficiency familiarity
    english fluency practical interest partner evolve
    business digital transformation learning internal undertaking taking continually
    encourage encouraging campaign contribution community innovation teamwork
    self-organisation whitepaper whitepapers think-tank think-tanks spheres
    experiment experimenting engineer engineers engineering growth
    """.split()
)

SENIORITY_MARKERS = {
    "senior", "lead", "principal", "staff", "head", "director", "manager",
    "vp", "chief", "junior", "mid-level", "entry", "intern", "graduate",
    "associate", "executive", "officer", "leadership",
}

# Bucket headings inside a posting. Order matters: first match wins.
REQUIREMENT_HEADINGS = (
    "requirement", "qualification", "must have", "must-have", "must-haves",
    "essential", "what you'll need", "what you will need", "what we're looking for",
    "what we are looking for", "who you are", "about you", "your profile",
    "skills and experience", "experience required", "you will need",
    "your background", "minimum criteria", "criteria", "we need",
    # "What you need to have" is the standard partner to "Nice to have", and it
    # was missing — so an entire requirements list inherited the DUTY bucket from
    # the preceding "What you'll do" and was weighted at 1.5 instead of 3.0.
    # Prose form of "must have". Safe as a contains key: "What you need to do" is
    # a duty heading and does not contain it.
    "need to have", "what you need to have", "what you'll need to have",
    "what you will need to have",
    "what you bring", "you'll bring", "you will bring", "your experience",
    "the ideal candidate", "what we offer you",
    "tools", "tooling", "technical requirements", "skills", "experience of",
)
DUTY_HEADINGS = (
    "what you'll be doing", "what you will be doing", "responsibilit",
    "the role", "your role", "day to day", "day-to-day", "duties",
    "what you'll do", "what you will do", "key accountabilities", "accountabilities",
    "the opportunity", "about the role", "in this role", "job description",
    "scope of the role",
)
NICE_HEADINGS = (
    "desirable", "nice to have", "nice-to-have", "preferred", "bonus",
    "advantageous", "good to have", "it would be great", "stand out",
    "not essential", "a plus", "ideally",
    "it would also be great", "it would also be good", "would also be great",
)
# Headings whose content is not a skill signal: employer branding, benefits,
# values, logistics. Everything here is weighted as body text.
BENEFITS_HEADINGS = (
    "benefit", "what we offer", "we offer", "perks", "package", "compensation",
    "salary", "our culture", "about us", "equal opportunit", "how to apply",
    "interview process", "why join", "inclusion", "diversity", "belonging",
    "behaviour", "behavior", "competenc", "values", "our mission", "who we are",
    "life at", "location", "logistics", "practicalities", "additional information",
    "pension", "wellbeing", "well-being", "holiday", "our people", "the team",
    # "Work in a Way That Works for You" and "Working Pattern" are the standard
    # Workday / LinkedIn ways-of-working headings, and BOTH failed on the Elsevier
    # advert (2026-09-22) in different ways — see context/06-engine-bugs.md.
    # "Working Pattern" WAS recognised as a heading but had no bucket, so it
    # inherited `requirements` and its content was weighted 3.0x. Worse, "Work in
    # a Way That Works for You" is 7 words, so it failed the short-noun-phrase
    # path, was never recognised as a heading at all, and stayed as CONTENT in
    # `requirements` — where its title-cased words became priority terms and
    # manufactured a requirement gap called "way" plus a bogus evidenced term
    # "works". A title-cased unrecognised heading is a priority-term generator,
    # which is why the fix is a lexicon entry rather than a length heuristic:
    # these keys make the line match BOTH `looks_like_heading` (via the at_start
    # keyword path) and `heading_bucket` (here, checked first), so it is consumed
    # and its section correctly becomes `other`.
    "work in a way", "working pattern",
    "next steps", "more about", "the opportunity",
    # LinkedIn adverts commonly split the pitch from the role with bare headings
    # like these. They are not branding keywords, but if they return None they
    # INHERIT the previous bucket — so "WHY US" sitting after "YOU WILL" would
    # have its culture prose weighted as duties.
    "why us", "why work", "why people", "the process", "our process",
    "hiring process", "recruitment process", "what do people think",
    # Third-person recruiter adverts phrase the same section differently, and an
    # unrecognised heading INHERITS the previous bucket — so "What They Offer"
    # sitting after "Your Skills & Experience" had salary, pension, healthcare
    # and holiday ALL weighted as requirements. "The Company" is the same hazard
    # when it appears after the requirements section rather than before it.
    "what they offer", "they offer", "what's on offer", "on offer",
    "we can offer", "what we can offer", "the offer",
    "the company", "about the company", "who they are", "our client",
    "the client", "about our client", "the business", "about the business",
)

# Bare-two-word headings that the tables above cannot carry safely, because as
# prefix or substring keys they also open ordinary requirement BULLETS
# ("You have five years of Java…", "You will work closely with engineering…").
#
# Exact match, not `startswith`: "you have" is a heading, "You have five years of
# experience in test automation" is content, and only exact equality separates
# them without a length heuristic that would break on some other posting.
EXACT_REQUIREMENT_HEADINGS = frozenset({
    "you have", "you are", "you'll have", "you will have", "you will need",
    "you'll need", "you're", "you are:", "your skills", "your skills and experience",
})
EXACT_DUTY_HEADINGS = frozenset({
    "you will", "you'll be", "you will be", "your duties", "your duties will include",
    "the role will involve", "what you'll be responsible for",
})


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------

def normalise(text: str) -> str:
    """Lowercase and flatten punctuation, keeping term-internal characters."""
    text = text.lower()
    text = text.replace("\u2019", "'").replace("\u2018", "'")
    text = text.replace("\u201c", '"').replace("\u201d", '"')
    text = text.replace("\u2013", "-").replace("\u2014", "-")
    # Keep + # . for c++, c#, node.js, .net; / and & become spaces so that
    # "CI/CD" and "P&L" match their spelled-out synonym keys.
    text = re.sub(r"[^a-z0-9+#.'\-\s]", " ", text)
    text = text.replace("/", " ").replace("&", " and ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("ies", "y"), ("sses", "ss"), ("ing", ""), ("edly", ""), ("ed", ""), ("es", ""), ("s", ""),
)


def stem(word: str) -> str:
    """Crude suffix stripper. Good enough to stop 'pipelines' reading as a gap."""
    if len(word) <= 3:
        return word
    for suffix, replacement in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            base = word[: -len(suffix)] + replacement
            if suffix in ("ing", "ed") and len(base) >= 3:
                if base[-1] == base[-2] and base[-1] not in "lsz":
                    base = base[:-1]
            return base
    return word


def stem_key(word: str) -> str:
    """Stem plus trailing-e strip, so 'warehousing'/'warehouse' converge."""
    return stem(word).rstrip("e") or word


def tokens_of(text: str) -> list[str]:
    """Normalised tokens with sentence punctuation stripped from the ends.

    `normalise` deliberately preserves '.' so that 'node.js' and '.net' survive.
    The side effect is that sentence-final words arrive as 'warehouse.' -- which
    then matches nothing, silently inventing gaps for every sentence-ending term
    in a real CV. Stripping the ends here fixes that without losing
    term-internal dots.
    """
    out: list[str] = []
    for tok in normalise(text).split():
        tok = tok.strip(".'-")
        if tok:
            out.append(tok)
    return out


def stemmed_text(text: str) -> str:
    return " ".join(stem_key(t) for t in tokens_of(text))


# --------------------------------------------------------------------------
# Term
# --------------------------------------------------------------------------

@dataclass
class Term:
    term: str
    count: int = 0
    in_requirements: int = 0
    in_duties: int = 0
    in_nice: int = 0
    is_skill: bool = False
    capitalised: bool = False
    in_title: bool = False
    surfaces: Counter = field(default_factory=Counter)

    @property
    def weight(self) -> float:
        """Requirements dominate, then duties, then raw frequency."""
        return (
            3.0 * self.in_requirements
            + 1.5 * self.in_duties
            + 1.0 * self.count
            + 0.5 * self.in_nice
        )

    @property
    def is_priority(self) -> bool:
        """Is this worth the reader's attention, or just prose?

        Two ways to qualify:

        * a recognised skill term (a lexicon hit) — in a requirements or duties
          section, or mentioned repeatedly
        * a capitalised, non-lexicon term — 'Ranorex', 'Jira', 'DevOps',
          'TestNG'. Tool and product names a lexicon will never cover.

        Ordinary lowercase prose never qualifies on its own, however often it
        appears. Without that rule a verbose posting buries its two real
        requirements under thirteen mentions of 'appropriate'.
        """
        # A tool named in the job title is definitionally central, however
        # little the body mentions it. 'Senior Test Automation Engineer
        # (TeamCity)' names TeamCity once, in the intro, and nowhere under
        # Requirements — without this it sank into the truncated background list
        # and the role's defining tool never appeared in the report.
        if self.in_title:
            return True
        if self.is_skill:
            return self.weight >= 2.5
        # Capitalised candidates must earn it by appearing in a requirements or
        # duties section. Otherwise employer branding ('Nucleus', mentioned
        # three times in the About us copy) would read as a required technology.
        return self.capitalised and (self.in_requirements > 0 or self.in_duties > 0)


# --------------------------------------------------------------------------
# Section splitting
# --------------------------------------------------------------------------

def _heading_probe(line: str) -> str:
    """Normalise a heading line for keyword matching.

    Hyphens and dashes become SPACES rather than being stripped. Stripping them
    turned "Nice-to-have" into the single token "nicetohave", which matched no
    table entry, so the heading returned None and **inherited the previous
    bucket** — every nice-to-have item on the Xe advert was weighted as a
    requirement (3.0) instead of a nice-to-have (0.5). "Must-have" failed the
    same way and was saved only by luck: it inherited `requirements` from the
    preceding "What We're Looking For".

    Every hyphenated key in the tables has a spaced twin ("must-have" / "must
    have", "nice-to-have" / "nice to have", "day-to-day" / "day to day"), so
    collapsing to spaces satisfies both spellings with no new entries.
    """
    probe = line.strip().lower().replace("\u2013", "-").replace("\u2014", "-")
    probe = probe.replace("-", " ")
    probe = re.sub(r"[^a-z0-9' ]", "", probe)
    return re.sub(r"\s+", " ", probe).strip()


def heading_bucket(probe: str, at_start: bool = False) -> str | None:
    """Map a heading probe to a bucket, or None if unrecognised.

    `at_start` requires the keyword to open the probe, which is what keeps
    content bullets out: "Experience with performance requirements" merely
    contains a heading keyword, whereas "Requirements" starts with one.
    """
    def hits(keys: tuple[str, ...]) -> bool:
        if at_start:
            return any(probe.startswith(k) for k in keys)
        return any(k in probe for k in keys)

    # "Why <anything>" is a culture/benefits pitch, matched as a PATTERN rather
    # than added to a keyword list, because that list can never be complete: the
    # employer's own name is the variable.
    #
    # BENEFITS_HEADINGS already carried "why us", "why work", "why people" and
    # "why join" — clearly the same intent, and the comment above them describes
    # this exact hazard — yet "Why Version 1?" still slipped past. An
    # unrecognised heading inherits the previous bucket, so 2,251 characters of
    # pension, life-assurance and profit-share prose were weighted as
    # REQUIREMENTS (found 2026-09-23). That inflated the requirement denominator
    # from 17 terms to 60 and reported "wellbeing", "life", "balance" and
    # "scheme" as requirement-level gaps — the opposite error to the one it
    # replaced, and just as wrong.
    #
    # Gated on `at_start`, so a bullet that merely contains the word "why" is
    # untouched. A heading that OPENS with "why" is inviting the reader to
    # consider the employer; it is never stating a requirement.
    if at_start and probe.startswith("why"):
        return "other"
    if hits(BENEFITS_HEADINGS):
        return "other"
    if hits(REQUIREMENT_HEADINGS):
        return "requirements"
    if hits(DUTY_HEADINGS):
        return "duties"
    if hits(NICE_HEADINGS):
        return "nice"
    # Last, and exact-match only. See EXACT_REQUIREMENT_HEADINGS for why these
    # cannot live in the tables above.
    if probe in EXACT_REQUIREMENT_HEADINGS:
        return "requirements"
    if probe in EXACT_DUTY_HEADINGS:
        return "duties"
    return None


def looks_like_heading(line: str, isolated: bool | None = None) -> bool:
    """Is this line a section heading rather than posting content?

    Tuned against nine real postings. Three failure modes, and they pull in
    different directions:

    * Too strict, and a whole posting loses its sections. Requiring EVERY word to
      be capitalised rejected "What you'll bring", "The benefits" and "About the
      job" — all real headings — which dropped an entire posting into `other`
      with no section weighting and silently reduced the gap report to raw
      frequency.
    * Too loose, and requirement bullets get eaten as headings. Allowing any
      short capitalised line swallowed "Experience using Docker to aid testing"
      and "Experience using Maven to run your automated tests".
    * TOO LOOSE IN A DIFFERENT WAY, and this one dropped requirements outright.
      The gap report for the Lead Test Engineer advert recommended Azure DevOps
      and never mentioned it: "Azure DevOps / TFS" is three capitalised words, so
      the short-noun-phrase path claimed it as a heading and `split_sections`
      DISCARDED the line. "SQL" and "Cloud-native and microservices
      architectures" went the same way — one genuine gap and two real strengths,
      all invisible because they happened to look like headings.

    Hence: a hard length cap on the generic path, a separate and deliberately
    narrower path for lines that OPEN with a recognised heading keyword,
    comma/terminal-punctuation guards that exclude sentence-shaped bullets, and
    — for the short-noun-phrase path only — a requirement that the line be
    ISOLATED by blank lines.

    `isolated` is what separates the two cases typographically. A heading in a
    pasted advert has a blank line above and below it; a bullet in a list has
    neither. Callers that cannot supply the signal may omit it (None), which
    preserves the older, looser behaviour — `split_sections` supplies it.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith("#"):
        return True
    probe = _heading_probe(stripped)
    words = probe.split()
    if not words:
        return False
    # "Requirements:" / "Key Responsibilities:" — a trailing colon is a strong
    # heading signal on a short line.
    if stripped.endswith(":") and len(words) <= 6:
        return True
    # ALL CAPS heading, e.g. "YOUR PROFILE". Also gated on isolation: a bare
    # ALL-CAPS line in a list is usually a BULLET ("SQL", "AWS", "TFS"), and
    # dropping those hides strengths as well as gaps. Keyword-bearing caps
    # headings are still caught by the keyword path below, so gating here costs
    # almost nothing — "YOUR PROFILE" matches REQUIREMENT_HEADINGS anyway.
    letters = [c for c in stripped if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and len(words) <= 6 and isolated is not False:
        return True
    if stripped.endswith((".", ",", ";", ":")) or "," in stripped:
        return False
    # A short heading whose HEAD NOUN is a recognised keyword: "Key
    # Responsibilities", "Technical Skills", "Your Requirements". English puts
    # the head noun LAST in a noun-phrase heading, so the `at_start` prefix test
    # further down misses every one of these -- and the isolation guard below
    # cannot rescue them either, because a real paste puts the first bullet
    # directly under the heading with no blank line between.
    #
    # Left unfixed, the heading is not dropped (it was never recognised), so it
    # stays a CONTENT line in whatever bucket preceded it and its whole section
    # is mis-weighted. On the Anson McCade advert that put "Key Responsibilities"
    # and all eight of its bullets into `other`, leaving `duties` empty -- which
    # demotes every term named only in the responsibilities from priority to
    # background. Same class of failure as the seven earlier heading bugs: silent,
    # and it understates rather than invents.
    #
    # Gated three ways, and each gate earns its place:
    #   * `len(words) <= 4` -- noun-phrase headings are short.
    #   * the final word CAPITALISED in the source line. This is what separates a
    #     heading from a requirement bullet that merely ends in a keyword:
    #     "Experience of gathering requirements" and "Ownership of requirements"
    #     are content, and this gate alone rejects both.
    #   * `heading_bucket(..., at_start=True)` on that word, so the bullets that
    #     failure mode 3 lost ("Azure DevOps / TFS", "SQL", "Cloud-native and
    #     microservices architectures") stay lost to the heading path exactly as
    #     before: none of them ends in a keyword.
    #
    # Audited across all 20 postings in the repo before landing: three lines
    # change, and all three are genuine headings previously mis-bucketed
    # ("Key Responsibilities" -> duties, "Technical Skills" -> requirements,
    # "Competitive Salary & Discretionary Bonus" -> nice).
    #
    # Known limitation, deliberately not closed: the gate needs the head noun
    # capitalised, so an advert that writes "Key responsibilities" in lower case
    # is still missed unless blank-line isolation catches it.
    if len(words) <= 4:
        tokens = stripped.split()
        if tokens and tokens[-1][:1].isupper():
            if heading_bucket(words[-1], at_start=True) is not None:
                return True
    # Short capitalised noun phrase: "The benefits", "Tools & Methods".
    # The isolation guard is the whole point — see the third failure mode above.
    if len(words) <= 4 and isolated is not False:
        first = next((w for w in stripped.split() if w[:1].isalpha()), "")
        if first and first[:1].isupper():
            return True
    # Longer line that OPENS with a heading keyword, e.g.
    # "It would also be great if you have experience in some of the following".
    if len(words) <= 14 and heading_bucket(probe, at_start=True) is not None:
        return True
    return False


def split_sections(text: str) -> dict[str, str]:
    """Bucket posting lines into requirements / duties / nice / other.

    Weighting by section is the difference between a useful report and a bag of
    buzzwords: a term under 'Requirements' is a filter, the same term under
    'Benefits' is decoration.

    Two decisions worth knowing about:

    * **Substring, not prefix matching.** Real postings head their sections
      'Typical Job Responsibilities', 'Tools & Methods', 'Expected Professional
      Behaviours'. Requiring the section word to come first matches almost none
      of them, and an unmatched posting silently loses all section weighting.

    * **Unrecognised headings inherit the current bucket.** Postings nest
      sub-headings inside sections ('Automation Test Development' under
      'Typical Job Responsibilities'). Resetting to `other` on every
      unrecognised heading would demote every nested sub-section, so only a
      *recognised* heading changes the bucket. The cost is that an unrecognised
      major transition keeps its predecessor's bucket, which is why
      BENEFITS_HEADINGS is deliberately broad — branding, values, behaviours,
      benefits and logistics all have keywords.
    """
    buckets: dict[str, list[str]] = {"requirements": [], "duties": [], "nice": [], "other": []}
    current = "other"
    lines = text.splitlines()
    last = len(lines) - 1
    for i, line in enumerate(lines):
        # A heading is isolated by blank lines; a bullet in a list is not. That
        # typographic signal is what stops "Azure DevOps / TFS" and "SQL" being
        # discarded as headings — see looks_like_heading's third failure mode.
        isolated = (i == 0 or not lines[i - 1].strip()) and (
            i == last or not lines[i + 1].strip()
        )
        if looks_like_heading(line, isolated=isolated):
            probe = _heading_probe(line)
            bucket = heading_bucket(probe, at_start=True) or heading_bucket(probe)
            if bucket is not None:
                current = bucket
            # else: unrecognised sub-heading — inherit the enclosing bucket.
            continue
        buckets[current].append(line)
    return {k: "\n".join(v) for k, v in buckets.items()}


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------

def order_phrases(phrases) -> list[str]:
    """Order lexicon phrases for masked matching: longest first, ties alphabetical.

    **The tie-break is the entire point of this function existing.** Phrase
    matching masks each match out of the text before trying the next phrase, so
    ORDER CHANGES RESULTS — not merely presentation. Sorting by length alone
    leaves phrases of equal length in whatever order the input iterable happened
    to have, and when that input is a set of strings the order depends on
    PYTHONHASHSEED, which CPython randomises per process.

    Consequence of getting this wrong, measured on 2026-09-22: four identical
    invocations of `gap.py` on one posting produced two different reports. The
    Elsevier advert extracted `communication` (evidenced) in one process and
    `verbal communication` (reported as a priority gap) in another. Every report
    in the repo had been generated under an arbitrary hash seed.

    It also silently corrupted the repo's own quality check: AGENTS.md's
    reproducibility test compares a stored report against a freshly generated one,
    so a nondeterministic extractor makes that test report FALSE staleness.

    Kept as a named function with the phrase collection as a parameter so the
    ordering is unit-testable with a synthetic equal-length input — which is the
    only way to test it, since a test process has a single fixed hash seed and
    cannot reproduce the cross-process variation that caused the bug.
    """
    return sorted(phrases, key=lambda p: (-len(p), p))


def extract_terms(
    text: str,
    extra_stop: frozenset[str] = frozenset(),
) -> dict[str, Term]:
    """Pull candidate keywords out of a block of posting text."""
    terms: dict[str, Term] = {}
    norm = normalise(text)

    # Words that appear with a leading capital somewhere in the source, i.e.
    # candidate proper nouns: 'Ranorex', 'Jira', 'DevOps', 'TestNG'. Sentence-
    # initial words land here too, which is why BOILERPLATE filtering runs first.
    capitalised = {
        w.lower().strip(".'-")
        for w in re.findall(r"\b[A-Z][A-Za-z0-9+#.\-]*", text)
    }

    def bump(surface: str, canonical: str, skill: bool, named: bool = False) -> None:
        if not canonical:
            return
        entry = terms.setdefault(
            canonical, Term(term=canonical, is_skill=skill)
        )
        entry.count += 1
        entry.is_skill = entry.is_skill or skill
        entry.capitalised = entry.capitalised or named
        entry.surfaces[surface] += 1

    # 1. Multi-word lexicon phrases, longest first so 'feature store' beats a
    #    bare 'feature'. Matched spans are then MASKED OUT of the text before
    #    the unigram pass, so a phrase's constituent words are not reported
    #    again as standalone terms.
    #
    #    Masking at the matched position, rather than keeping a global set of
    #    "covered" words, matters more than it looks. A global set deletes a
    #    genuinely required standalone term merely because one of its words
    #    appeared inside a phrase somewhere else: this posting requires
    #    "Integration" as a core testing technique, and also says "continuous
    #    integration pipeline" — the global approach silently dropped the
    #    requirement.
    remaining = norm
    # LONGEST FIRST, AND — CRITICALLY — WITH A DETERMINISTIC TIE-BREAK.
    #
    # This was `sorted({...}, key=len, reverse=True)` until 2026-09-22, and that
    # single missing tie-break made the ENTIRE gap report nondeterministic. The
    # input is a SET, whose iteration order for strings depends on PYTHONHASHSEED,
    # which CPython randomises per process. Sorting by length alone leaves phrases
    # of EQUAL length in that random relative order, and because each match is
    # MASKED OUT of the text before the next one is tried, order changes which
    # phrase wins and therefore what gets extracted at all.
    #
    # Measured, not theorised: four identical invocations of gap.py on the same
    # posting produced two different reports. The visible symptom on the Elsevier
    # advert was `communication` (evidenced) in one run and `verbal communication`
    # (a priority gap) in another — the same advert, the same code, different
    # answers.
    #
    # Why this matters beyond tidiness: AGENTS.md's reproducibility check compares
    # a stored report against a freshly generated one, and a nondeterministic
    # extractor makes that check produce FALSE staleness — it fired exactly once
    # during the run that found this, and looked like a report-freshness problem
    # rather than an engine one. Any report in the repo was generated under an
    # arbitrary hash seed, so a term could always have landed one table over.
    #
    # The fix sorts by (-length, phrase), so equal-length phrases resolve
    # alphabetically and every process agrees.
    phrases = order_phrases({p for p in SYNONYMS if " " in p})
    for phrase in phrases:
        # Tolerate plurals on the final word: postings write "branching
        # strategies" and "test cases" where the lexicon holds the singular.
        if phrase.endswith("y"):
            pattern = rf"(?<![a-z0-9]){re.escape(phrase[:-1])}(?:y|ies)(?![a-z0-9])"
        else:
            pattern = rf"(?<![a-z0-9]){re.escape(phrase)}s?(?![a-z0-9])"
        hits = len(re.findall(pattern, remaining))
        if not hits:
            continue
        for _ in range(hits):
            bump(phrase, SYNONYMS[phrase], True)
        remaining = re.sub(pattern, " ", remaining)

    # 2. Single tokens and short acronyms, over the phrase-masked text.
    for raw in remaining.split():
        tok = raw.strip(".'-")
        if not tok or tok in STOPWORDS or tok in extra_stop:
            continue
        named = tok in capitalised
        if tok in SYNONYMS:
            bump(tok, SYNONYMS[tok], True, named)
            continue
        if tok in BOILERPLATE:
            continue
        if re.fullmatch(r"[a-z]{2,6}", tok):            # acronyms: sql, aws, iam
            bump(tok, tok, False, named)
        elif len(tok) >= 4 and re.search(r"[a-z]", tok):
            bump(tok, tok, False, named)

    return terms


def analyse_post(
    post_text: str,
    extra_stop: frozenset[str] = frozenset(),
    title: str = "",
) -> tuple[dict[str, Term], dict[str, dict[str, Term]]]:
    """Return (all terms, per-section terms) for a posting.

    `extra_stop` is normally the employer's name and initials, which are noise.
    But employer names collide with domain terms more often than you'd think: a
    posting for a "Temenos Core Banking" role has 'temenos', 'core' and
    'banking' as its single most important requirement.

    So a stop-term is reinstated when it appears under a requirements or duties
    heading **and is a recognised skill term**. Requiring both is what separates
    the two cases: 'temenos' is a lexicon skill, whereas 'DT', 'Dunstan' and
    'Thomas' are the employer's own name, which appears throughout the duties
    text by construction. Reinstating on section position alone would resurrect
    the very noise this suppresses.
    """
    sections = split_sections(post_text)

    demanded = set(tokens_of(sections["requirements"] + " " + sections["duties"]))
    effective_stop = frozenset(
        t for t in extra_stop if not (t in demanded and t in SYNONYMS)
    )

    def stops(*extra: str) -> frozenset[str]:
        return frozenset(effective_stop | {e.lower() for e in extra if e})

    terms = extract_terms(post_text, stops())
    req = extract_terms(sections["requirements"], stops())
    duties = extract_terms(sections["duties"], stops())
    nice = extract_terms(sections["nice"], stops())

    for canonical, entry in terms.items():
        entry.in_requirements = req.get(canonical, Term(term=canonical)).count
        entry.in_duties = duties.get(canonical, Term(term=canonical)).count
        entry.in_nice = nice.get(canonical, Term(term=canonical)).count

    # Terms that only surfaced inside a section still belong in the report.
    for canonical, entry in {**nice, **duties}.items():
        if canonical not in terms:
            entry.in_nice = nice.get(canonical, Term(term=canonical)).count
            entry.in_duties = duties.get(canonical, Term(term=canonical)).count
            terms[canonical] = entry

    # Terms from the job title are always in scope, even if the posting body
    # never repeats them.
    for canonical, entry in extract_terms(title, effective_stop).items():
        existing = terms.get(canonical)
        if existing is None:
            terms[canonical] = entry
            existing = entry
        existing.in_title = True

    by_section = {"requirements": req, "duties": duties, "nice": nice}
    return terms, by_section


# --------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------

def variants(word: str) -> set[str]:
    """Every form of a token worth comparing on.

    Hyphen and space handling matters: postings write 'REST-assured' and 'Page
    Object Model', CVs write 'RestAssured' and 'Site-Prism Page Object Model'.
    Collapsing separators on both sides makes those compare equal instead of
    inventing a gap.
    """
    forms = {word, stem(word), stem_key(word)}
    for form in list(forms):
        if "-" in form:
            forms.add(form.replace("-", ""))
            forms.add(form.replace("-", " "))
            # Decompose compounds too, so 'sprint-based' still matches 'sprint'.
            forms.update(part for part in form.split("-") if part)
    return forms - {""}


def _cv_index(cv_text: str) -> tuple[set[str], str]:
    """Build (token set, stem-keyed string) for the CV side of a comparison.

    Keeping the raw tokens alongside the stems matters. Naive stemming is not
    reversible -- 'process' stems to 'proces' while 'processes' stems to
    'process' -- so a one-sided comparison invents gaps. Comparing the posting
    term's raw *and* stemmed forms against the CV's raw *and* stemmed forms
    makes the match symmetric.
    """
    token_set: set[str] = set()
    for tok in tokens_of(cv_text):
        token_set |= variants(tok)
    return token_set, " " + stemmed_text(cv_text) + " "


# Parts of a hyphenated compound that carry no meaning on their own. Without
# this, `variants()` decomposition makes a compound match on ANY part, so
# "front-to-back" matched a CV that merely contained the word "to".
HYPHEN_STOPWORDS = frozenset({
    "a", "an", "and", "of", "to", "for", "or", "the", "in", "on", "with", "as",
})


def _compound_forms(parts: list[str]) -> set[str]:
    """The three separator styles a hyphenated compound may be written in.

    Mirrors what `variants()` emits for a hyphenated CV token, so a compound
    candidate and a CV compound meet in the same vocabulary.
    """
    return {"-".join(parts), " ".join(parts), "".join(parts)}


def _compound_adjacent(parts: list[str], haystack: str) -> bool:
    """Is this compound present as a contiguous run in the stemmed CV text?

    Needed alongside `_compound_forms` because the CV may write the compound as
    two ordinary words, in which case no single token carries the spaced form:
    the master's "stakeholder management" tag tokenises to two tokens, so
    "stakeholder-management" is invisible to set membership alone.

    Separators are optional (a hyphen, a space, or nothing), which also absorbs
    `variants()`'s collapsing. Caveat: the haystack flattens sentence
    punctuation, so two parts that merely happen to sit either side of a full
    stop would match. That requires one compound's parts to land on a sentence
    boundary, and no posting in this repo triggers it -- re-checked after the
    change, every term that moved moved DOWNWARD.
    """
    key = r"[- ]?".join(re.escape(stem_key(p)) for p in parts)
    return re.search(rf"(?<![a-z0-9]){key}(?![a-z0-9])", haystack) is not None


def _term_in(canonical: str, surfaces: set[str], token_set: set[str], haystack: str) -> bool:
    for candidate in {canonical} | surfaces:
        cand_norm = normalise(candidate).strip()
        if not cand_norm:
            continue
        if " " in cand_norm:
            key = stemmed_text(cand_norm)
            if key and re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", haystack):
                return True
        else:
            # A hyphenated compound must match on EVERY meaningful part, not any.
            # ANY-semantics is a false-positive generator on two fronts:
            #   * "front-to-back" hit because the CV contains "to" — a preposition.
            #   * "non-functional" hit on "functional", silently crediting Ed with
            #     a discipline he has never worked in. A negated compound matching
            #     its own root is the worst case, because the report then says he
            #     is covered for precisely what he is not.
            # A compound built only from very short words ("end-to-end", "no-go")
            # carries no specific information, so matching it proves nothing —
            # "end-to-end" was still hitting on the bare token "end".
            #
            # The compound rule applies ONLY when the candidate really is a
            # compound. Applying the four-character floor to a plain token broke
            # every short one — `SQL`, `AWS`, `API`, `Git` all stopped matching,
            # which is why the headline self-test caught it. Single tokens take
            # the original path untouched.
            raw_parts = [p for p in cand_norm.split("-") if p]
            parts = [p for p in raw_parts if p not in HYPHEN_STOPWORDS]
            if len(raw_parts) > 1:
                if not parts or max(len(p) for p in parts) < 4:
                    continue
                # ...and the surviving parts must be ADJACENT, i.e. the compound
                # must appear as a unit. Requiring only that each part appears
                # SOMEWHERE still produced false positives, because unrelated
                # words can supply the parts by coincidence: "risk-based" was
                # EVIDENCED against a CV containing "Risk Profiling" (risk) and
                # "Kubernetes-based tooling" (based), which silently credited Ed
                # with risk-based test design — the exact gap the Acme Digital Play run had
                # already declined to claim. Both parts were present and the
                # compound was not.
                #
                # Comparing the joined forms against `token_set` rather than
                # reconstructing adjacency from the stemmed haystack is
                # deliberate. `variants()` already emits all three separator
                # styles for every CV token ("microservice-based" contributes
                # "microservice-based", "microservice based" and
                # "microservicebased"), so the question "does the CV contain
                # this compound?" reduces to set membership in the same
                # vocabulary the rest of the matcher uses. Adjacency is thus
                # judged consistently on both sides, and unlike a part-wise
                # regex it cannot be skewed by stemming a hyphenated token as a
                # single word.
                #
                # All parts, stopwords included, are joined for the spaced and
                # collapsed forms, because the CV may legitimately write the
                # whole compound ("state-of-the-art").
                for seq in (raw_parts, parts):
                    if _compound_forms(seq) & token_set:
                        return True
                    if _compound_adjacent(seq, haystack):
                        return True
                continue
            if variants(cand_norm) & token_set:
                return True
    return False


def coverage(terms: dict[str, Term], cv_text: str) -> tuple[list[Term], list[Term]]:
    """Partition posting terms into (evidenced, missing) against the CV text."""
    token_set, haystack = _cv_index(cv_text)
    present: list[Term] = []
    missing: list[Term] = []
    for entry in terms.values():
        surfaces = {s for s in entry.surfaces if s and s != entry.term}
        if _term_in(entry.term, surfaces, token_set, haystack):
            present.append(entry)
        else:
            missing.append(entry)
    present.sort(key=lambda t: (-t.weight, t.term))
    missing.sort(key=lambda t: (-t.weight, t.term))
    return present, missing


def coverage_pct(present: list[Term], missing: list[Term]) -> float:
    total = len(present) + len(missing)
    return 100.0 * len(present) / total if total else 0.0


def split_priority(terms: list[Term]) -> tuple[list[Term], list[Term]]:
    """Separate must-address terms from background colour."""
    priority = [t for t in terms if t.is_priority]
    other = [t for t in terms if not t.is_priority]
    return priority, other
