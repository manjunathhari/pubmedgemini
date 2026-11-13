"""The patient-details check, ported from Incisive.

Part 1 ports Incisive `crates/incisive-app/tests/deid.rs` one for one (commit 12630f7): each
test is named after the Incisive test it mirrors, and the three tables (BLOCK, PASS,
ROUND4_PROBES) are Incisive's rows, copied mechanically. Offsets are character indices here
(Incisive's are bytes), so slices are taken on the string itself.

Part 2 covers what Python's `re` reads differently from Rust's `regex` (word boundaries,
Unicode classes, case folding, digits in other scripts, `$`, number parsing, byte windows).
Each expected value was checked against Incisive's own `deid.rs` built with its regex crate
versions (1.13.1, regex-syntax 0.8.11) on 11 Nov 2026; a plain Python port would get them
wrong.

Part 3 ports the two `tests/ask.rs` cases for a blocked question (`identifiers_blocked_before_gate`,
`hidden_characters_blocked_before_gate`) at the one place the check is wired in: `create_run`.

Every patient-like string here is invented (Incisive's own test strings, or made up).
"""

import time
from itertools import pairwise

import pytest

from pubmedgemini.deid import (
    BLOCKED_MESSAGE,
    KINDS,
    Finding,
    blocked_message,
    check_question,
    describe,
    hint,
    looks_like_range,
    scan,
)


def kinds(text: str) -> tuple[str, ...]:
    """Distinct kinds found in `text`, in Incisive's enum order."""
    found = {f.kind for f in scan(text)}
    return tuple(k for k in KINDS if k in found)


# Incisive tests/deid.rs `BLOCK`, `PASS` and `ROUND4_PROBES`, row for row (expected kinds
# in Incisive's enum order; an empty tuple means the text must pass).

INCISIVE_BLOCK = [
    ("Aadhaar 2345 6789 0123", ('aadhaar',)),
    ("Aadhaar 2345-6789-0123", ('aadhaar',)),
    ("uid 234567890123 on file", ('aadhaar',)),
    ("PAN ABCDE1234F", ('pan',)),
    ("pan no. BNZPM2501F given", ('pan',)),
    ("ph +91 98450 12345", ('phone',)),
    ("mobile 9845012345", ('phone',)),
    ("call 080-2502 4444", ('phone',)),
    ("tel (080) 2502 4444", ('phone',)),
    ("+919845012345", ('phone',)),
    ("whatsapp 09845012345", ('phone',)),
    ("attender 98450-12345", ('phone',)),
    ("email ramesh.k@example.com", ('email',)),
    ("write to dr_x+cases@example.co.in please", ('email',)),
    ("DOB 12/03/1971", ('calendar_date',)),
    ("DOB 5/6/1990", ('calendar_date',)),
    ("seen on 2024-03-12", ('calendar_date',)),
    ("operated 12-03-2024", ('calendar_date',)),
    ("admitted 05.06.19", ('calendar_date',)),
    ("on 12 March 2024 she", ('calendar_date',)),
    ("March 12, 2024 discharge", ('calendar_date',)),
    ("scheduled 12th Mar", ('calendar_date',)),
    ("seen 1st Nov 2023", ('calendar_date',)),
    ("born 3rd May", ('calendar_date',)),
    ("UHID: MH-2231", ('hospital_id',)),
    ("IP no 44718", ('hospital_id',)),
    ("MRN 0012345", ('hospital_id',)),
    ("OP no. B1234", ('hospital_id',)),
    ("Patient ID: 99823", ('hospital_id',)),
    ("Reg no 55/2231", ('hospital_id',)),
    ("IPD 7788", ('hospital_id',)),
    ("uhid #MH8812", ('hospital_id',)),
    ("Mrs. Lakshmi, 52F", ('person_name',)),
    ("patient Ramesh had", ('person_name',)),
    ("Mr Suresh Kumar underwent", ('person_name',)),
    ("Shri Gopal presented", ('person_name',)),
    ("Smt. Devi with gallstones", ('person_name',)),
    ("Baby of Anitha, 3 kg", ('person_name',)),
    ("Kumari Priya, 8 y/o", ('person_name',)),
    ("Master Arjun, 6 y/o", ('person_name',)),
    ("Ms. Rao, post-op day 3", ('person_name',)),
    ("pt called Meena", ('person_name',)),
    ("named Prakash", ('person_name',)),
    ("name: Rajesh", ('person_name',)),
    ("Patient Name: Ramesh Kumar", ('person_name',)),
    ("the patient Kavita was", ('person_name',)),
    ("#12, 4th Cross, Indiranagar 560038", ('address',)),
    ("House no 14, Jayanagar", ('address',)),
    ("Flat no. 302 Prestige Towers", ('address',)),
    ("lives at 4th Main Road", ('address',)),
    ("HSR Layout 2nd sector", ('address',)),
    ("pincode 600001", ('address',)),
    ("Pin 560038", ('address',)),
    ("door no 7/2, Vijayanagar", ('address',)),
    ("Mrs. Lakshmi UHID 4421 ph 9845012345", ('phone', 'hospital_id', 'person_name')),
    ("Aadhaar 2345 6789 0123 and PAN ABCDE1234F", ('aadhaar', 'pan')),
    ("Bengaluru-560038", ('address',)),
    ("Whitefield, Bangalore 560066", ('address',)),
    ("Bangalore 560 038", ('address',)),
    ("PIN: 560038", ('address',)),
    ("PIN code 560038", ('address',)),
    ("4th Cross, Indiranagar", ('address',)),
    ("UHID no. 4471", ('hospital_id',)),
    ("uhid- MH 12345", ('hospital_id',)),
    ("MR no 4471", ('hospital_id',)),
    ("MR No. 4471", ('hospital_id',)),
    ("MRD no 4471", ('hospital_id',)),
    ("Hosp no 4471", ('hospital_id',)),
    ("Hospital No: 4471", ('hospital_id',)),
    ("IP 2231/24", ('hospital_id',)),
    ("Bed 12, IP 4471", ('hospital_id',)),
    ("Mr.Ramesh", ('person_name',)),
    ("MRS LAKSHMI 52F", ('person_name',)),
    ("SHRI GOPAL", ('person_name',)),
    ("my patient, Ramesh, 52M", ('person_name',)),
    ("Name - Ramesh Kumar", ('person_name',)),
    ("s/o Ramesh", ('person_name',)),
    ("w/o Suresh Kumar", ('person_name',)),
    ("D/O Ramesh", ('person_name',)),
    ("Mrs. Hartmann 52F", ('person_name',)),
    ("Mr. Lewis, 60M", ('person_name',)),
    ("Mr. Miles 62M", ('person_name',)),
    ("Flat 302, Prestige Towers", ('address',)),
    ("Plot 12, Sector 5", ('address',)),
    ("No. 45, MG Road, Bengaluru", ('address',)),
    ("45 MG Road", ('address',)),
    ("Koramangala 5th Block", ('address',)),
    ("Door No 12", ('address',)),
    ("98450\xa012345", ('phone',)),
    ("98450\U0000201312345", ('phone',)),
    ("2345\xa06789\xa00123", ('aadhaar',)),
    ("2345\U0000202f6789\U000020090123", ('aadhaar',)),
    ("12\U00002013Mar\U000020132024", ('calendar_date',)),
    ("12\xa0March\xa02024", ('calendar_date',)),
    ("ramesh\U0000ff20example.com", ('email',)),
    ("98 450 12345", ('phone',)),
    ("2345.6789.0123", ('aadhaar',)),
    ("2345  6789  0123", ('aadhaar',)),
    ("12-Mar-2024", ('calendar_date',)),
    ("12-Mar-24", ('calendar_date',)),
    ("S/O. RAMESH", ('person_name',)),
    ("W/o Sri Ramesh", ('person_name',)),
    ("c/o Ramesh, 45M", ('person_name',)),
    ("c/o Kamal 45 yrs", ('person_name',)),
    ("MR RAMESH KUMAR", ('person_name',)),
    ("Bangalore 560038 / Karnataka", ('address',)),
    ("PAN abcde1234f", ('pan',)),
    ("PAN no: abcde1234f", ('pan',)),
    ("Patient - Ramesh Kumar", ('person_name',)),
    ("S/o Gopal", ('person_name',)),
    ("s/o Vishal", ('person_name',)),
    ("s/o Kunal, 23M", ('person_name',)),
    ("s/o Sonal", ('person_name',)),
    ("s/o Kunal", ('person_name',)),
    ("w/o Kamal", ('person_name',)),
    ("D/o Sonia", ('person_name',)),
    ("S/O. GOPAL", ('person_name',)),
    ("W/O GOPAL", ('person_name',)),
    ("S/O GOPAL KRISHNA", ('person_name',)),
    ("s/o Gopal Krishna", ('person_name',)),
    ("w/o Gopal Kumar", ('person_name',)),
    ("c/o Vishal Kumar", ('person_name',)),
    ("c/o Kunal Shah", ('person_name',)),
    ("w/o Kunal Shah", ('person_name',)),
    ("w/o Sonia Gandhi", ('person_name',)),
    ("c/o Kamala, aged 45", ('person_name',)),
    ("s/o:Ramesh", ('person_name',)),
    ("S/o- Ramesh", ('person_name',)),
    ("son of Ramesh", ('person_name',)),
    ("wife of Suresh Kumar", ('person_name',)),
    ("s/o Dr. Ramesh", ('person_name',)),
    ("s/o late Ramaiah", ('person_name',)),
    ("h/o Ramesh, 60M", ('person_name',)),
    ("s/o Late Sri Ramaiah", ('person_name',)),
    ("Name: Kunal Shah", ('person_name',)),
    ("CRP 12, mobile 9845012345", ('phone',)),
    ("WBC 11000 count, call 9845012345", ('phone',)),
    ("attender 9845012345 per son", ('phone',)),
    ("ph 9845012345 MG Road", ('phone',)),
    ("contact 98450 12345 ml-ward", ('phone',)),
    ("Bengaluru 560038 and phone below", ('address',)),
    ("resides Whitefield 560066 with wife", ('address',)),
    ("Address: Whitefield, Bengaluru 560066 for review", ('address',)),
    ("House No. 45 G Block", ('address',)),
    ("Door No 12 G Floor", ('address',)),
    ("Flat 302 G Wing, Prestige", ('address',)),
    ("IP 4471 M 45y", ('hospital_id',)),
    ("IP 4471 m", ('hospital_id',)),
    ("from Whitefield 560066 presents with pain", ('address',)),
    ("Address: Whitefield, Bengaluru 560066 presents for review", ('address',)),
    ("Kadugodi, Bengaluru 560067 visits OPD", ('address',)),
    ("Bengaluru 560038 attends clinic", ('address',)),
    ("MRS SAROJAMMA, 67 F", ('person_name',)),
    ("call on 98450 12 345", ('phone',)),
    ("s/o Om", ('person_name',)),
    ("S/O MUNIYAPPA OCCUPATION: AGRICULTURIST", ('person_name',)),
    ("S/O NAGESH GOWDA DIAGNOSIS: RT INGUINAL HERNIA", ('person_name',)),
    ("D/O SHIVALINGAPPA DIAGNOSIS CA BREAST", ('person_name',)),
    ("s/o Venkatappa Station Road, Hoskote", ('person_name',)),
    ("s/o Thimmaiah Admission Diagnosis Acute Appendicitis", ('person_name',)),
    ("Son of Muniswamy Diagnosis: SAIO", ('person_name',)),
    ("Wife of Chandrashekar Occupation Housewife", ('person_name',)),
    ("s/o Ramappa Station Road", ('person_name',)),
    ("c/o Puttaswamy, 56 yrs", ('person_name',)),
    ("c/o Puttaswamy, 56 Years, Male", ('person_name',)),
    ("w/o Kariyappa, 48 yrs", ('person_name',)),
    ("Patient Nancy admitted", ('person_name',)),
    ("Pt Soma, 45F", ('person_name',)),
    ("Patient Roma Devi", ('person_name',)),
    ("Name: Nancy Thomas", ('person_name',)),
    ("s/o Thimmaiah Diagnosis Acute Appendicitis", ('person_name',)),
    ("S/O MUNIYAPPA DIAGNOSIS INGUINAL HERNIA", ('person_name',)),
    ("s/o Muniyappa Diagnosis Hernia", ('person_name',)),
    ("S/O MUNIYAPPA IMPRESSION ACUTE APPENDICITIS", ('person_name',)),
    ("Wife of Chandrashekar Diagnosis Carcinoma", ('person_name',)),
    ("D/O Venkatappa Diagnosis Carcinoma Breast", ('person_name',)),
    ("S/O MUNIYAPPA WITH INGUINAL HERNIA", ('person_name',)),
    ("c/o Puttaswamy, 56 years of age", ('person_name',)),
    ("W/O MUNIRAJU, 54 years of age", ('person_name',)),
    ("c/o Puttaswamy , 56 years, history of hernia", ('person_name',)),
    ("NAME: RAMAKKA", ('person_name',)),
    ("Name: RAMAKKA", ('person_name',)),
    ("NAME: Ramakka", ('person_name',)),
    ("NAME : RAMAKKA", ('person_name',)),
    ("PATIENT NAME: RAMAKKA", ('person_name',)),
    ("NAME: RAMESH", ('person_name',)),
    ("NAME: RAMAKKA AGE/SEX: 45/F", ('person_name',)),
    ("Pt. Name: RAMAKKA", ('person_name',)),
    ("Pt Name - Ramakka", ('person_name',)),
    ("Patient Name : RAMAKKA DEVI", ('person_name',)),
    ("ph 98450\U0000200b12345", ('unusual_characters',)),
    ("ph 9845\xad012345", ('unusual_characters',)),
    ("ph \U0000ff19\U0000ff18\U0000ff14\U0000ff15\U0000ff10\U0000ff11\U0000ff12\U0000ff13\U0000ff14\U0000ff15", ('unusual_characters',)),
    ("Aadhaar 2345\U0000200b6789\U0000200b0123", ('unusual_characters',)),
    ("Mrs\U0000200b. Lakshmi", ('unusual_characters',)),
    ("UHID\U0000200b: MH-2231", ('unusual_characters',)),
    ("DOB 12\U0000221503\U000022151971", ('unusual_characters',)),
    ("ph 98450\U0000206012345", ('unusual_characters',)),
    ("ph 98450\U0000feff12345", ('unusual_characters',)),
    ("ph \U0000202e5432105489", ('unusual_characters',)),
    ("ph \U00000969\U00000968\U00000967", ('unusual_characters',)),
    ("ph \U00000cef\U00000cee\U00000cea", ('unusual_characters',)),
    ("DOB 12\U0000204403\U000020441971", ('unusual_characters',)),
    ("DOB 12\U000029f803\U000029f81971", ('unusual_characters',)),
    ("at 10\U0000223630", ('unusual_characters',)),
    ("at 10\U0000fe5530", ('unusual_characters',)),
    ("at 10\U0000fe1330", ('unusual_characters',)),
    ("ph 98450\U0000201212345", ('unusual_characters',)),
    ("DOB 12\U0000201003\U000020101971", ('unusual_characters',)),
    ("DOB 12\U0000201103\U000020111971", ('unusual_characters',)),
    ("ph 98450\U0000201512345", ('unusual_characters',)),
    ("ph 98450\U0000221212345", ('unusual_characters',)),
    ("DOB 12\U0000202703\U000020271971", ('unusual_characters',)),
    ("ph 98450\U0000ff0d12345", ('unusual_characters',)),
    ("ph 98450\U000020792345", ('unusual_characters',)),
    ("ph \U00002079\U00002078\U00002074\U00002075", ('unusual_characters',)),
    ("ph 98450\U0000201312345", ('phone',)),
    ("DOB 12\U0000201303\U000020131971", ('calendar_date',)),
    ("ph 98450\U0000034f12345", ('phone',)),
    ("ph 98450\U0000fe0f12345", ('phone',)),
    ("ph 98450\U0000115f12345", ('unusual_characters',)),
    ("ph 98450\U0000316412345", ('unusual_characters',)),
    ("ph 98450\U0000280012345", ('unusual_characters',)),
    ("ph 98450\x0112345", ('unusual_characters',)),
    ("ph 98450\x7f12345", ('unusual_characters',)),
    ("ph 98450\x8512345", ('unusual_characters',)),
    ("ph 98450\U0000033212345", ('phone',)),
    ("ph 98450\U000020e312345", ('phone',)),
    ("Mrs\U00000301. Lakshmi", ('person_name',)),
    ("UHID\U00000301: MH-2231", ('hospital_id',)),
    ("Mrs.\U00000301 Lakshmi", ('person_name',)),
    ("UHID\U0000a789 MH-2231", ('unusual_characters',)),
    ("UHID\U000002d0 MH-2231", ('unusual_characters',)),
    ("UHID\U00000589 MH-2231", ('unusual_characters',)),
    ("UHID\U000005c3 MH-2231", ('unusual_characters',)),
    ("UHID\U0000fe30 MH-2231", ('unusual_characters',)),
    ("UHID\U00000703 MH-2231", ('unusual_characters',)),
    ("UHID\U00002e35 MH-2231", ('unusual_characters',)),
    ("UHID MH\U000020132231", ('unusual_characters',)),
    ("UHID MH\U000020112231", ('unusual_characters',)),
    ("UHID MH\U000022122231", ('unusual_characters',)),
    ("UHID MH\U000020102231", ('unusual_characters',)),
    ("UHID MH\U000020142231", ('unusual_characters',)),
    ("DOB 12\xb703\xb71971", ('unusual_characters',)),
    ("DOB 12\U0000202403\U000020241971", ('unusual_characters',)),
    ("DOB 12\U0000300203\U000030021971", ('unusual_characters',)),
    ("DOB 12\U0000257103\U000025711971", ('unusual_characters',)),
    ("DOB 12\U0000303303\U000030331971", ('unusual_characters',)),
    ("DOB 12\U0000033803\U000003381971", ('unusual_characters',)),
    ("ph 98450\U0000fe6312345", ('unusual_characters',)),
    ("ph 98450\U00002e3a12345", ('unusual_characters',)),
    ("ph 98450\U00002e3b12345", ('unusual_characters',)),
    ("ph 98450\U0000fe5812345", ('unusual_characters',)),
    ("ph 98450\U0000204312345", ('unusual_characters',)),
    ("ph 98450\U0000200712345", ('unusual_characters',)),
    ("ph 98450\U0000200212345", ('unusual_characters',)),
    ("ph 98450\U0000300012345", ('unusual_characters',)),
    ("ph 98450\U0000205f12345", ('unusual_characters',)),
    ("ph 98450\U0000168012345", ('unusual_characters',)),
    ("ph 98450\xa012345", ('phone',)),
    ("ph 98450\U0000202f12345", ('phone',)),
    ("ramesh\U0000fe6bexample.com", ('unusual_characters',)),
    ("ramesh\U0000ff20example.com", ('email',)),
    ("\U0000041crs. Lakshmi", ('unusual_characters',)),
    ("PAN \U0001d400\U0001d401CDE1234F", ('unusual_characters',)),
    ("\U00000930\U0000094b\U00000917\U00000940 52", ('unusual_characters',)),
    ("Clavien-Dindo \U00002162a", ('unusual_characters',)),
    ("leak? \U0001f914", ('unusual_characters',)),
    ("10\U00002153 cm", ('unusual_characters',)),
    ("platelets 65000\U0000201380000", ('unusual_characters',)),
    ("40\U0000201360 mg", ('unusual_characters',)),
]
INCISIVE_ROUND4_PROBES = [
    ("from Whitefield 560066 presents with pain", ('address',)),
    ("Address: Whitefield, Bengaluru 560066 presents for review", ('address',)),
    ("Kadugodi, Bengaluru 560067 visits OPD", ('address',)),
    ("Bengaluru 560038 attends clinic", ('address',)),
    ("c/o Lt Inguinal Swelling", ()),
    ("c/o Lt Sided Abdominal Pain", ()),
    ("s/o Lt Inguinal Hernia", ()),
    ("Healed w/o Late Complications", ()),
    ("s/o Late Sri Ramaiah", ('person_name',)),
    ("s/o late Ramaiah", ('person_name',)),
    ("CT s/o Intestinal Obstruction", ()),
    ("S/O INTESTINAL OBSTRUCTION", ()),
    ("s/o Achalasia (Cardia)", ()),
    ("s/o Duodenal Ulcer Perforation", ()),
    ("s/o Intussusception", ()),
    ("s/o Rectal Growth", ()),
    ("s/o Umbilical Hernia", ()),
    ("s/o Perianal Fistula", ()),
    ("s/o Peritoneal Carcinomatosis", ()),
    ("Findings s/o Sigmoid Volvulus", ()),
    ("s/o Hepatic Abscess", ()),
    ("s/o Pancreatic Pseudocyst", ()),
    ("s/o Gastric Outlet Obstruction", ()),
    ("s/o Appendicular Lump", ()),
    ("s/o Mesenteric Ischemia", ()),
    ("s/o Koch's Abdomen", ()),
    ("s/o Free Fluid", ()),
    ("S/O SUBACUTE INTESTINAL OBSTRUCTION", ()),
    ("s/o Gas Under Diaphragm", ()),
    ("s/o Fatty Liver", ()),
    ("s/o Calculous Cholecystitis", ()),
    ("S/o Gopal", ('person_name',)),
    ("s/o Vishal", ('person_name',)),
    ("s/o Kunal", ('person_name',)),
    ("s/o Sonal", ('person_name',)),
    ("w/o Kamal", ('person_name',)),
    ("D/o Sonia", ('person_name',)),
    ("S/O. GOPAL", ('person_name',)),
    ("S/O GOPAL KRISHNA", ('person_name',)),
    ("s/o Kunal, 23M", ('person_name',)),
    ("h/o Previous Surgery", ()),
    ("h/o Similar Complaints", ()),
    ("c/o Heaviness Over Left Groin", ()),
    ("Consent from Son of Patient", ()),
    ("Wife of Patient informed", ()),
    ("h/o Smoking 20 years", ()),
    ("h/o Alcohol Intake 15 years", ()),
    ("h/o Surgery 10 yrs ago", ()),
    ("h/o Asthma 20 years", ()),
    ("h/o Tobacco Chewing 30 years", ()),
    ("h/o Trauma 12 years back", ()),
    ("h/o Ramesh, 60M", ('person_name',)),
    ("c/o Kamala, aged 45", ('person_name',)),
    ("MRS SAROJAMMA, 67 F", ('person_name',)),
    ("ABG pH 7.31, Plt 65000-80000", ()),
    ("Flat 8 MM polyp", ()),
    ("Flat 2 CM lesion", ()),
    ("IP 500 MG CISPLATIN", ()),
    ("IP 1 g ceftriaxone", ()),
    ("HBV DNA 7800000000 IU/mL", ()),
    ("House No. 45 G Block", ('address',)),
    ("Door No 12 G Floor", ('address',)),
    ("Flat 302 G Wing, Prestige", ('address',)),
    ("IP 4471 M 45y", ('hospital_id',)),
    ("IP 4471 m", ('hospital_id',)),
    ("call on 98450 12 345", ('phone',)),
    ("s/o Om", ('person_name',)),
    ("S/O MUNIYAPPA OCCUPATION: AGRICULTURIST", ('person_name',)),
    ("S/O NAGESH GOWDA DIAGNOSIS: RT INGUINAL HERNIA", ('person_name',)),
    ("D/O SHIVALINGAPPA DIAGNOSIS CA BREAST", ('person_name',)),
    ("s/o Venkatappa Station Road, Hoskote", ('person_name',)),
    ("s/o Thimmaiah Admission Diagnosis Acute Appendicitis", ('person_name',)),
    ("Son of Muniswamy Diagnosis: SAIO", ('person_name',)),
    ("Wife of Chandrashekar Occupation Housewife", ('person_name',)),
    ("s/o Ramappa Station Road", ('person_name',)),
    ("c/o Puttaswamy, 56 yrs", ('person_name',)),
    ("c/o Puttaswamy, 56 Years, Male", ('person_name',)),
    ("w/o Kariyappa, 48 yrs", ('person_name',)),
    ("Patient Nancy admitted", ('person_name',)),
    ("Pt Soma, 45F", ('person_name',)),
    ("Patient Roma Devi", ('person_name',)),
    ("Name: Nancy Thomas", ('person_name',)),
    ("s/o Sebaceous Cyst", ()),
    ("s/o Dermoid Cyst", ()),
    ("s/o Intestinal Malrotation", ()),
    ("s/o Arteriovenous Malformation", ()),
    ("s/o Paget Disease", ()),
    ("S/O CA RECTUM", ()),
    ("Impression: s/o Acute Appendicitis", ()),
    ("s/o Carcinoid Tumour", ()),
    ("s/o Ovarian Torsion", ()),
    ("c/o Giddiness, 2 years", ()),
    ("c/o Pain Abdomen, 10 years", ()),
    ("c/o Swelling, 10 years duration", ()),
    ("c/o Dyspepsia, 12 years", ()),
    ("c/o Heartburn, 15 years back", ()),
    ("h/o Epilepsy, 12 yrs", ()),
    ("CT s/o Annular Pancreas", ()),
    ("s/o Thimmaiah Diagnosis Acute Appendicitis", ('person_name',)),
    ("S/O MUNIYAPPA DIAGNOSIS INGUINAL HERNIA", ('person_name',)),
    ("s/o Muniyappa Diagnosis Hernia", ('person_name',)),
    ("S/O MUNIYAPPA IMPRESSION ACUTE APPENDICITIS", ('person_name',)),
    ("Wife of Chandrashekar Diagnosis Carcinoma", ('person_name',)),
    ("D/O Venkatappa Diagnosis Carcinoma Breast", ('person_name',)),
    ("S/O MUNIYAPPA WITH INGUINAL HERNIA", ('person_name',)),
    ("Impression: s/o Acute Appendicitis", ()),
    ("s/o Sigmoid Diverticulitis with pericolic abscess", ()),
    ("USG s/o Acute Calculous Cholecystitis with GB wall edema", ()),
    ("s/o Desmoid Tumour of anterior abdominal wall", ()),
    ("c/o Puttaswamy, 56 years of age", ('person_name',)),
    ("W/O MUNIRAJU, 54 years of age", ('person_name',)),
    ("c/o Puttaswamy , 56 years, history of hernia", ('person_name',)),
    ("c/o Dyspepsia, 12 years of history", ()),
    ("c/o Pain, 2 months of duration", ()),
    ("NAME: RAMAKKA", ('person_name',)),
    ("Name: RAMAKKA", ('person_name',)),
    ("NAME: Ramakka", ('person_name',)),
    ("NAME : RAMAKKA", ('person_name',)),
    ("PATIENT NAME: RAMAKKA", ('person_name',)),
    ("NAME: RAMESH", ('person_name',)),
    ("NAME: RAMAKKA AGE/SEX: 45/F", ('person_name',)),
    ("Name of the procedure: Whipple", ()),
    ("NAME OF SURGERY: LAP CHOLECYSTECTOMY", ()),
    ("Brand name: Prolene mesh", ()),
    ("Drug name: Tranexamic acid", ()),
    ("Trial name: ROLARR", ()),
    ("w/o Recurrence, 10 years later", ()),
    ("c/o Backache, 10 yrs", ()),
    ("Biopsy s/o Signet Ring Cell Carcinoma", ()),
]
INCISIVE_PASS = [
    "52 y/o M, T3N1M0 rectal ca, POD 3, Hb 9.8, CEA 12.4",
    "Whipple vs PPPD in 2019 trial",
    "Clavien-Dindo IIIa leak on day 5",
    "ASA 3, BMI 31",
    "120 mg bd for 6 weeks",
    "52-year-old male",
    "52F with biliary colic",
    "6 weeks post-op, POD 3, day 5",
    "ECOG 1, ASA 2, POD 5",
    "Grade 2 complication at 6/12",
    "Pt underwent lap chole",
    "pt with T3 tumour",
    "Patient had ileus",
    "Pt Hb 9.8",
    "PT INR 1.2 Normal",
    "Patient Safety in robotic surgery",
    "patient-reported outcome measures",
    "patient selection for Whipple",
    "Master the learning curve",
    "Miss rate of polyps",
    "Mr. Hartmann's procedure for perforated diverticulitis",
    "named Whipple procedure",
    "called Hartmann",
    "Child-Pugh B, Dukes C, Billroth II",
    "Heller myotomy vs Nissen",
    "platelets 150000",
    "WBC 11200",
    "Hb 9.8",
    "platelets 150000/\xb5L",
    "TLC 450000 cells",
    "count 120000",
    "Hb 9.8, platelets 150000 per mm3",
    "WBC 11200 and platelets 250000",
    "CA 19-9 1200 U/mL",
    "CEA 12.4 ng/mL",
    "accession 9845012345678",
    "ID 12345678901234",
    "dose 1500000 units",
    "in 2019",
    "in 2019 trial",
    "in March 2024",
    "1/2",
    "3/12 follow-up",
    "BP 120/80",
    "3 may be required",
    "survival 12.3 months",
    "trocar layout 5 mm",
    "nerve block 3 ml",
    "2 stage procedure",
    "pooling #3 sample",
    "IPD analysis of 8 trials",
    "MRN is recorded in the notes",
    "T3N1M0 ypT2N0, R0 resection",
    "a cohort of 150000 patients",
    "sample size 120000 patients",
    "150000 cases",
    "n = 240000",
    "NSQIP 2015-2019 data of 234567 patients",
    "AFP 400000",
    "ferritin 120000",
    "D-dimer 500000",
    "viral load 250000 copies",
    "CA 19-9 > 100000",
    "cost 250000 rupees",
    "Bilirubin 123456 mg",
    "12 cross-sectional studies",
    "3 cross-over trials",
    "the 2nd cross-match",
    "4th cross-sectional",
    "Mr. Hartmann's procedure",
    "Ivor Lewis operation",
    "Mr Hartmann pouch",
    "Patient Blood Management",
    "Patient Controlled Analgesia",
    "Ms Excel",
    "Master Class",
    "Miss Rate",
    "patient Murphy sign positive",
    "patient Courvoisier",
    "patient Boerhaave",
    "patient Charlson index 4",
    "patient Caprini 5",
    "patient Kaplan-Meier",
    "patient Gleason 7",
    "patient Fontaine IV",
    "c/o abdominal pain",
    "C/O Abdominal pain for 3 days",
    "w/o jaundice",
    "#12 French catheter, main bile duct",
    "port #3, main working port",
    "6th main, leak",
    "c/o Dyspepsia",
    "c/o Heartburn",
    "c/o Haematemesis",
    "c/o Hematemesis",
    "c/o Malena",
    "c/o Pruritus",
    "c/o Reflux",
    "c/o Regurgitation",
    "c/o Obstipation",
    "c/o Hernia",
    "c/o Haemorrhoids",
    "c/o Painless jaundice",
    "c/o Severe pain RIF",
    "c/o Sudden onset pain",
    "c/o Intermittent pain",
    "c/o Inguinal swelling",
    "c/o Altered bowel habits",
    "c/o Fullness after meals",
    "c/o Passing blood",
    "USG s/o Cholecystitis",
    "CT s/o Perforation",
    "HPE s/o Adenocarcinoma",
    "Imaging s/o Malignancy",
    "USG s/o Grade III fatty liver",
    "USG s/o Appendicitis",
    "w/o Contrast",
    "CT abdomen w/o Contrast",
    "h/o Diabetes",
    "MR PELVIS WITH CONTRAST",
    "MR ENTEROGRAPHY",
    "MR CHOLANGIOPANCREATOGRAPHY",
    "MRS SCORE",
    "MR ABDOMEN",
    "MR LIVER",
    "Individual Patient-Level Data",
    "Patient-Centred Care",
    "Patient-Specific Instruments",
    "Flat 8 mm polyp in sigmoid",
    "Flat 2 cm lesion",
    "IP 500 mg cisplatin",
    "Mr. Kocher manoeuvre was done",
    "platelets 65000-80000",
    "65000-80000/cumm",
    "Approximately 250000 procedures",
    "Overall 234567 admissions",
    "Leucocytes 120000",
    "Medicare 123456 beneficiaries",
    "Japan 150000 gastrectomies",
    "Case No. 2, A 60-year-old male",
    "Table No. 3, The results",
    "s/o Mucocele",
    "s/o Abscess",
    "s/o Ascites",
    "s/o Fistula",
    "s/o Volvulus",
    "s/o Leak",
    "s/o Small Bowel Obstruction",
    "CT s/o Bowel Wall Thickening",
    "d/o Hirschsprung",
    "w/o Injury",
    "w/o Drain",
    "Robotic TME done w/o Conversion",
    "C/O PAIN ABDOMEN",
    "S/O ACUTE APPENDICITIS",
    "c/o Mild pain at port site",
    "MRCP s/o Choledocholithiasis",
    "platelets 65000-80000",
    "Platelets 85000-120000/cumm",
    "65000-80000/cumm",
    "Overall 120000 robotic cases",
    "IP 200 ml/hr fluids",
    "Flat 30-degree scope",
    "Patient, Seen in OPD",
    "c/o Lt Inguinal Swelling",
    "c/o Lt Sided Abdominal Pain",
    "s/o Lt Inguinal Hernia",
    "Healed w/o Late Complications",
    "CT s/o Intestinal Obstruction",
    "S/O INTESTINAL OBSTRUCTION",
    "s/o Achalasia (Cardia)",
    "s/o Duodenal Ulcer Perforation",
    "s/o Intussusception",
    "s/o Rectal Growth",
    "s/o Umbilical Hernia",
    "s/o Perianal Fistula",
    "s/o Peritoneal Carcinomatosis",
    "Findings s/o Sigmoid Volvulus",
    "s/o Hepatic Abscess",
    "s/o Pancreatic Pseudocyst",
    "s/o Gastric Outlet Obstruction",
    "s/o Appendicular Lump",
    "s/o Mesenteric Ischemia",
    "s/o Koch's Abdomen",
    "s/o Free Fluid",
    "S/O SUBACUTE INTESTINAL OBSTRUCTION",
    "s/o Gas Under Diaphragm",
    "s/o Fatty Liver",
    "s/o Calculous Cholecystitis",
    "h/o Previous Surgery",
    "h/o Similar Complaints",
    "c/o Heaviness Over Left Groin",
    "Consent from Son of Patient",
    "Wife of Patient informed",
    "h/o Smoking 20 years",
    "h/o Alcohol Intake 15 years",
    "h/o Surgery 10 yrs ago",
    "h/o Asthma 20 years",
    "h/o Tobacco Chewing 30 years",
    "h/o Trauma 12 years back",
    "ABG pH 7.31, Plt 65000-80000",
    "Flat 8 MM polyp",
    "Flat 2 CM lesion",
    "IP 500 MG CISPLATIN",
    "IP 1 g ceftriaxone",
    "HBV DNA 7800000000 IU/mL",
    "s/o Sebaceous Cyst",
    "s/o Dermoid Cyst",
    "s/o Intestinal Malrotation",
    "s/o Arteriovenous Malformation",
    "s/o Paget Disease",
    "S/O CA RECTUM",
    "Impression: s/o Acute Appendicitis",
    "s/o Carcinoid Tumour",
    "s/o Ovarian Torsion",
    "c/o Giddiness, 2 years",
    "c/o Pain Abdomen, 10 years",
    "c/o Swelling, 10 years duration",
    "c/o Dyspepsia, 12 years",
    "c/o Heartburn, 15 years back",
    "h/o Epilepsy, 12 yrs",
    "CT s/o Annular Pancreas",
    "s/o Sigmoid Diverticulitis with pericolic abscess",
    "USG s/o Acute Calculous Cholecystitis with GB wall edema",
    "s/o Desmoid Tumour of anterior abdominal wall",
    "c/o Dyspepsia, 12 years of history",
    "c/o Pain, 2 months of duration",
    "Name of the procedure: Whipple",
    "NAME OF SURGERY: LAP CHOLECYSTECTOMY",
    "Brand name: Prolene mesh",
    "Drug name: Tranexamic acid",
    "Trial name: ROLARR",
    "Procedure name: Whipple",
    "PROCEDURE NAME: LAP CHOLECYSTECTOMY",
    "Generic name: Paracetamol",
    "Trade Name: Ultrapro",
    "Device name: Stapler",
    "Study Name: ROLARR",
    "Name: Unknown",
    "PATIENT NAME: WITHHELD",
    "w/o Recurrence, 10 years later",
    "c/o Backache, 10 yrs",
    "Biopsy s/o Signet Ring Cell Carcinoma",
    "Crohn\U00002019s disease",
    "5 \xb5g fentanyl",
    "37.5 \xb0C fever",
    "\U00002265 2 cm margin",
    "\U00002264 5 mm",
    "\U000003b2-blocker",
    "caf\xe9-au-lait spots",
    "BMI 32 kg/m\xb2",
    "BSA 1.8 m\xb2, 4 cm\xb3",
    "platelets 150 \xd710\U00002079/L",
    "RBC 4.5 \xd710\xb9\xb2/L",
    "1\xbd hours",
    "\xbd tablet",
    "mean 12 \xb1 3",
    "HbA1c 7.2 %, SpO\U00002082 94%",
    "ICG in robotic low anterior resection \U00002014 evidence for leak?",
    "dose 5-10 mg",
    "temperature -2 \xb0C",
    "\U000003b2\U00002010blocker",
    "mild \U00002010 moderate",
    "pain - worse",
    "\U0000201cleak\U0000201d \U00002026 \U00002018rates\U00002019",
    "\U00002022 item one",
    "\U00002192 next step",
    "Ki-67 index",
    "caf\xe9-au-lait spots",
    "cafe\U00000301-au-lait spots",
    "m\xb2",
    "\xd710\U00002079/L",
    "SpO\U00002082",
    "Ca\xb2\U0000207a",
    "HCO\U00002083\U0000207b",
    "Ki-67",
    "\U000003b1-fetoprotein",
    "TNM 8\U00001d57\U000002b0 edition",
    "\U000020b9300",
    "\U00002022 bullet point",
    "resection \U00002014 evidence",
    "mild\U00002013moderate pain",
    "\U000003b2\U00002011blocker",
    "5 \U00002013 10 mg",
    "pros\xb7cons",
    "\xa0leading NBSP and word\U0000202fgap",
    "\xb0 \xb1 \xd7 \xf7 \xb5 \U00002265 \U00002264 \U00002260 \U00002248 \U0000223c",
    "\U00002192 \U00002190 \U00002191 \U00002193 \U00002018x\U00002019 \U0000201cy\U0000201d \U00002026",
    "\xbc dose, \xbe dose, \xae \U00002122 \xa9 \xa73 \U000020ac5 \xa35 3\U00002030",
    "na\xefve, Sj\xf6gren, M\xe9n\xe9trier, \xc6, stra\xdfe",
    "\U00000394T, \U000003a3, \U000003c0",
    "tab\tand\nnewline\r\n",
]


# ---------- Part 1: Incisive tests/deid.rs, one for one ----------


def test_must_block_table():
    assert len(INCISIVE_BLOCK) >= 25
    bad = [f"{t!r}: want {w}, got {kinds(t)}" for t, w in INCISIVE_BLOCK if kinds(t) != w]
    assert not bad, "mis-classified blocks:\n" + "\n".join(bad)


def test_must_pass_table():
    assert len(INCISIVE_PASS) >= 25
    bad = [f"{t!r}: unexpectedly flagged {scan(t)}" for t in INCISIVE_PASS if scan(t)]
    assert not bad, "false positives:\n" + "\n".join(bad)


def test_table_is_big_enough():
    assert len(INCISIVE_BLOCK) + len(INCISIVE_PASS) >= 60
    assert (len(INCISIVE_BLOCK), len(INCISIVE_PASS), len(INCISIVE_ROUND4_PROBES)) == \
        (271, 292, 129)


def test_offsets_point_at_the_match():
    t = "ph 9845012345 now"
    f = scan(t)
    assert len(f) == 1 and t[f[0].start:f[0].end] == "9845012345"
    # Non-ASCII text before the match: Incisive's offsets are bytes; these are characters.
    t = "αβγ é Mrs. Lakshmi"
    f = scan(t)
    assert len(f) == 1 and f[0].kind == "person_name"
    assert t[f[0].start:f[0].end] == "Mrs. Lakshmi"
    t = "Ménétrier αβ 080-2502 4444 é, email a@example.org"
    f = scan(t)
    assert all(0 <= x.start < x.end <= len(t) for x in f)
    assert t[f[0].start:f[0].end] == "080-2502 4444"
    assert t[f[1].start:f[1].end] == "a@example.org"


def test_findings_are_sorted_by_start():
    f = scan("email a@example.org then PAN ABCDE1234F then ph 9845012345")
    assert len(f) == 3 and all(a.start <= b.start for a, b in pairwise(f))


def test_overlaps_are_merged():
    # HospitalId and Phone overlap on the same digits: one finding, union span.
    t = "UHID: 9845012345"
    f = scan(t)
    assert len(f) == 1, f
    assert t[f[0].start:f[0].end] == t
    # No two findings may ever overlap.
    f = scan("#12, 4th Cross, Indiranagar 560038, Mrs. Lakshmi UHID 4421 ph 9845012345")
    assert all(a.end <= b.start for a, b in pairwise(f))


def test_repeated_identifiers_are_all_found():
    assert len(scan("9845012345 and 9845012346")) == 2


def test_clean_and_empty_text():
    assert scan("") == []
    assert scan("Laparoscopic cholecystectomy for symptomatic cholelithiasis") == []


def test_kinds_serialise_snake_case():
    assert "calendar_date" in KINDS and "hospital_id" in KINDS and "aadhaar" in KINDS
    assert KINDS == ("aadhaar", "pan", "phone", "email", "calendar_date", "hospital_id",
                     "person_name", "address", "unusual_characters")


def test_every_kind_has_a_label():
    for k in KINDS:
        assert describe(k)
    assert describe("phone") == "phone number"
    assert describe("unusual_characters") == "hidden or unusual characters"
    assert hint("unusual_characters") == (
        "please retype the question without copy-pasted special characters "
        "(use plain hyphens, colons and spaces)")
    assert hint("phone") is None
    assert "unusual_characters" in KINDS


def test_unusual_characters_flag_each_maximal_run():
    t = "ph 98450\U0000200b\U0000200c\U0000200d12345 and 9845\xad012345"
    f = scan(t)
    assert len(f) == 2, f
    assert all(x.kind == "unusual_characters" for x in f)
    assert t[f[0].start:f[0].end] == "\U0000200b\U0000200c\U0000200d"
    assert t[f[1].start:f[1].end] == "\xad"
    # A full-width run is one finding over the whole run.
    t = "ph \U0000ff19\U0000ff18\U0000ff14 ok"
    f = scan(t)
    assert len(f) == 1 and t[f[0].start:f[0].end] == "\U0000ff19\U0000ff18\U0000ff14"
    # A look-alike dash is flagged only between digits.
    t = "12\U0000201003"
    f = scan(t)
    assert len(f) == 1 and t[f[0].start:f[0].end] == "\U00002010"


def test_marks_only_after_latin_or_greek_letters_at_most_two():
    assert scan("cafe\U00000301") == []
    assert scan("a\U00000301\U00000302b") == []
    t = "a\U00000301\U00000302\U00000303b"
    f = scan(t)
    assert len(f) == 1, f
    assert t[f[0].start:f[0].end] == "\U00000303"
    for t in ("1\U00000301", "-\U00000301", "\U00000301a", " \U00000301"):
        assert kinds(t) == ("unusual_characters",), t


def test_contextual_characters_only_away_from_digits():
    for c in ("\U00002013", "\U00002014", "\U00002010", "\U00002011", "\U00002212", "\xb7",
              "\xa0", "\U0000202f"):
        assert scan(f"a{c}b") == [], hex(ord(c))
        assert scan(f"a {c} b") == [], hex(ord(c))
        for t in (f"1{c}a", f"a{c}1", f"1{c}1"):
            assert kinds(t) == ("unusual_characters",), t


def test_superscripts_extending_a_number_are_flagged():
    assert kinds("ph 98450\xb9\xb2\xb3\U00002074\U00002075") == ("unusual_characters",)
    assert kinds("m\xb25") == ("unusual_characters",)


def test_every_format_character_is_flagged():
    # Every Unicode format (Cf) character in Incisive's list, not a hand-picked few.
    for cp in (0xAD, 0x600, 0x61C, 0x6DD, 0x70F, 0x180E, 0x200B, 0x200C, 0x200D, 0x200E,
               0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2060, 0x2061, 0x2062, 0x2063,
               0x2064, 0x2066, 0x2069, 0x206F, 0xFEFF, 0xFFF9, 0xFFFB, 0x110BD, 0x1D173,
               0xE0001, 0xE0041, 0xE007F):
        assert kinds(f"a{chr(cp)}b") == ("unusual_characters",), hex(cp)


def test_full_date_span_covers_the_year():
    for t in ("12-Mar-2024", "12-Mar-24"):
        f = scan(t)
        assert len(f) == 1, (t, f)
        assert (f[0].start, f[0].end) == (0, len(t)), t
    t = "seen \U00002013 12\U00002013Mar\U000020132024 ok"
    f = scan(t)
    assert t[f[0].start:f[0].end] == "12\U00002013Mar\U000020132024"


def test_month_year_stays_allowed():
    assert scan("in March 2024") == []


def test_six_digit_counts_scan_fast():
    text = "123456 mg " * 100_000
    t0 = time.monotonic()
    assert scan(text) == []
    # Incisive allows 300 ms in release and 10 s in a debug build. A question is at most
    # 4,000 characters, so this million-character text is 250 times the largest one; it is
    # here to catch quadratic behaviour.
    assert time.monotonic() - t0 < 10


def test_name_spans_cover_the_whole_name():
    for t, want in [
        ("s/o Late Sri Ramaiah", "s/o Late Sri Ramaiah"),
        ("Name: Kunal Shah", "Name: Kunal Shah"),
        ("seen s/o Gopal Krishna today", "s/o Gopal Krishna"),
        ("Mrs. Lakshmi UHID 4421", "Mrs. Lakshmi"),
        ("NAME: RAMAKKA AGE/SEX: 45/F", "NAME: RAMAKKA"),
        ("PATIENT NAME: RAMAKKA", "PATIENT NAME: RAMAKKA"),
    ]:
        f = scan(t)
        assert t[f[0].start:f[0].end] == want, t


def test_regression_corpus():
    rows = list(INCISIVE_BLOCK) + [(t, ()) for t in INCISIVE_PASS] + list(INCISIVE_ROUND4_PROBES)
    bad = [f"{t!r}: want {w}, got {kinds(t)}" for t, w in rows if kinds(t) != w]
    assert not bad, "regressions:\n" + "\n".join(bad)


# ---------- Part 2: where Python's re and Rust's regex differ ----------
# Each expected value is what Incisive's deid.rs returns (checked with the Rust build).


def test_word_boundary_after_a_fraction_is_rusts():
    # "½" is a word character to Python's \b and not to Rust's: a plain port would miss the
    # Aadhaar number and let the question through. "½" after an ASCII digit is allowed.
    assert kinds("Aadhaar 2345 6789 0123\xbd") == ("aadhaar",)
    assert kinds("PAN ABCDE1234F\xbd") == ("pan",)


def test_word_boundary_after_a_superscript_is_rusts():
    t = "ph 9845012345\xb2"
    assert kinds(t) == ("phone", "unusual_characters")
    assert [t[f.start:f.end] for f in scan(t)] == ["9845012345", "\xb2"]


def test_combining_mark_is_a_word_character_as_in_rust():
    # Rust's \b counts the mark as part of the word, so "Lakshmi" + mark is matched only by the
    # mark-free rescan; the finding then covers the mark too.
    t = "Mrs. Lakshmi\U00000301 had pain"
    f = scan(t)
    assert [(x.kind, t[x.start:x.end]) for x in f] == [("person_name", "Mrs. Lakshmi\U00000301")]
    # A mark after a digit is flagged, and the rescan's address covers it: one finding.
    t = "Pin 560038\U00000301"
    assert [(f.kind, t[f.start:f.end]) for f in scan(t)] == [("address", "560038\U00000301")]


def test_case_folding_is_rusts():
    # Rust's (?i) folds "ſ" to "s" (as Python does) ...
    assert kinds("Ho\U0000017fp no 4471") == ("hospital_id",)
    # ... but not "İ" or "ı" to "i", which Python's IGNORECASE does. Incisive lets these pass;
    # so does the port (a known gap, reported to the owner).
    assert kinds("UH\U00000130D: MH-2231") == ()
    assert kinds("UH\U00000131D: MH-2231") == ()


def test_white_space_is_unicode_white_space():
    # Python's \s matches U+001C; Rust's does not. The control character is flagged on its own.
    assert kinds("IP\x1c4471") == ("unusual_characters",)
    t = "IP\x1c4471"
    assert [(f.kind, f.start, f.end) for f in scan(t)] == [("unusual_characters", 2, 3)]


def test_digits_in_other_scripts_are_not_digits():
    # Rust's rules use [0-9]; other scripts' digits are flagged as unusual, never read as a
    # number (Arabic-Indic, Devanagari, full-width).
    for t in (("ph \U00000669\U00000668\U00000664\U00000665\U00000660\U00000661\U00000662"
               "\U00000663\U00000664\U00000665"),
              "Pin \U00000965\U0000096c\U00000966\U00000966\U00000969\U0000096e",
              "DOB 12/03/\U0000ff11\U0000ff19\U0000ff17\U0000ff11"):
        assert kinds(t) == ("unusual_characters",), t


def test_numbers_parse_as_rusts_u64():
    # str::parse::<u64> takes ASCII digits and a leading "+"; Python's int() also takes "_".
    assert looks_like_range("65000-80000") and looks_like_range("+100-200")
    assert not looks_like_range("1_000-2_000") and not looks_like_range("80000-65000")
    assert kinds("platelets 1_000-2_000") == ()


def test_windows_are_measured_in_bytes():
    # Incisive looks 16 bytes back for a PIN label. Each " –" is 4 bytes but 2 characters:
    # with three the label is inside the window, with four it is not (a character-based port
    # would still see it).
    assert kinds("pin \U00002013 \U00002013 \U00002013 560038") == ("address",)
    assert kinds("pin \U00002013 \U00002013 \U00002013 \U00002013 560038") == ()


def test_dollar_is_end_of_text():
    # A newline after "4th cross" still ends the address, as in Rust.
    assert kinds("4th cross\n") == ("address",)
    assert kinds("lives at 4th Cross") == ("address",)


def test_marks_on_both_engines_give_the_same_verdict():
    # Combining Latin small letters (U+0363..U+036F) are letters to Rust's is_alphabetic and
    # not to Python's isalpha; the mark-free rescan finds the name either way.
    assert kinds("s/o Ramesh\U00000363") == ("person_name",)
    assert kinds("Name: Ramesh\U00000345 Kumar") == ("person_name",)


# ---------- the outcome a page shows ----------


def test_blocked_message_is_incisives_with_this_apps_name():
    assert BLOCKED_MESSAGE == ("PubMed Ai found details that could identify a patient. "
                               "Please remove them and resend your question.")
    # ask.rs blocked_message: each distinct kind's hint, as a sentence, once.
    assert blocked_message(["phone", "person_name"]) == BLOCKED_MESSAGE
    assert blocked_message(["unusual_characters", "phone", "unusual_characters"]) == (
        BLOCKED_MESSAGE + " Please retype the question without copy-pasted special characters"
        " (use plain hyphens, colons and spaces).")


def test_check_question_gives_categories_and_counts_only():
    q = "Mrs. Lakshmi ph 9845012345 and 9845012346, robotic LAR, leak?"
    b = check_question(q)
    assert b.categories == (("phone", 2), ("person_name", 1))
    assert b.labels == ("phone number", "person's name") and b.total == 3
    assert b.message == BLOCKED_MESSAGE
    for leak in ("Lakshmi", "9845012345", "robotic"):
        assert leak not in repr(b), leak


def test_a_clean_question_passes_unchanged():
    assert check_question("In acute calculous cholecystitis, is early laparoscopic "
                          "cholecystectomy better than delayed surgery?") is None
    assert check_question("52 y/o M, T3N1M0 rectal ca, POD 3, Hb 9.8, CEA 12.4") is None


def test_findings_hold_kinds_and_offsets_only():
    f = scan("ph 9845012345")[0]
    assert f == Finding("phone", 3, 13)


# ---------- Part 3: the one place it is wired in (create_run), Incisive tests/ask.rs ----------


@pytest.fixture
def owner(migrated_pool):
    from pubmedgemini.identity import ensure_owner

    ensure_owner(migrated_pool, "owner@example.com")
    with migrated_pool.connection() as conn:
        return conn.execute("SELECT id FROM users").fetchone()[0]


def _stored(pool) -> dict[str, int]:
    with pool.connection() as conn:
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("runs", "run_steps", "run_searches", "run_seen", "run_papers",
                          "model_calls")}


def test_identifiers_blocked_before_gate(migrated_pool, owner):
    # Incisive tests/ask.rs identifiers_blocked_before_gate
    from pubmedgemini.runs import QuestionBlocked, create_run

    q = "Mrs. Lakshmi ph 9845012345, robotic LAR, leak?"
    with pytest.raises(QuestionBlocked) as e:
        create_run(migrated_pool, owner, q, "gemini-3.5-flash-lite")
    out = e.value.outcome
    names = [k for k, _ in out.categories]
    assert "phone" in names and "person_name" in names
    assert all(isinstance(n, int) and n > 0 for _, n in out.categories)
    for leak in ("9845012345", "Lakshmi"):
        assert leak not in repr(out) and leak not in str(e.value) and leak not in out.message
    assert "remove" in out.message
    assert set(_stored(migrated_pool).values()) == {0}  # no run, nothing stored


def test_hidden_characters_blocked_before_gate(migrated_pool, owner):
    # Incisive tests/ask.rs hidden_characters_blocked_before_gate
    from pubmedgemini.runs import QuestionBlocked, create_run

    q = "robotic LAR leak? ph 98450\U0000200b12345"
    with pytest.raises(QuestionBlocked) as e:
        create_run(migrated_pool, owner, q, "gemini-3.5-flash-lite")
    out = e.value.outcome
    assert out.categories == (("unusual_characters", 1),)
    assert out.labels == ("hidden or unusual characters",)
    assert out.message == (
        "PubMed Ai found details that could identify a patient. Please remove them and "
        "resend your question. Please retype the question without copy-pasted special "
        "characters (use plain hyphens, colons and spaces).")
    assert set(_stored(migrated_pool).values()) == {0}


def test_a_passed_question_is_stored_as_given(migrated_pool, owner):
    # Incisive stores the question as it came (no redaction, no rewriting).
    from pubmedgemini.runs import create_run, question_of

    q = "  52F, POD 3: is a CRP of 150 after robotic LAR a leak?  "
    rid = create_run(migrated_pool, owner, q, "gemini-3.5-flash-lite")
    assert question_of(migrated_pool, rid) == q


def test_a_blocked_question_reaches_neither_google_nor_the_literature(db_url, upstream):
    # The check is local: with the real app wiring (Gemini key, fake transports, a run starter
    # that would run at once), a blocked question makes no request to either side.
    from fastapi.testclient import TestClient
    from gemini_fakes import FAKE_KEY, FakeGemini

    from pubmedgemini.config import Settings
    from pubmedgemini.main import create_app
    from pubmedgemini.runner import InlineStarter
    from pubmedgemini.runs import QuestionBlocked, create_run

    g = FakeGemini([])
    app = create_app(Settings(app_env="dev", database_url=db_url,
                              owner_email="owner@example.com", gemini_api_key=FAKE_KEY),
                     lit_transport=upstream.transport, ncbi_gap_seconds=0,
                     gemini_transport=g.transport, gemini_sleep=lambda s: None,
                     run_starter=InlineStarter())
    with TestClient(app) as c:
        pool = c.app.state.pool
        with pool.connection() as conn:
            uid = conn.execute("SELECT id FROM users").fetchone()[0]
        for q in ("Mr. Ramesh Kumar, UHID 4471, 9845012345: mesh or suture?",
                  "s/o Gopal, DOB 12/03/1971, which repair?",
                  "Pin 560038, ramesh.k@example.com"):
            with pytest.raises(QuestionBlocked):
                rid = create_run(pool, uid, q, "gemini-3.5-flash-lite")
                c.app.state.run_starter.start(rid)  # never reached
        assert set(_stored(pool).values()) == {0}
    assert g.sent == [] and upstream.sent == []
