"""
Transaction Categorization Engine
Ported from Scripts/categorize_transactions.py for use in the FastAPI backend.

Priority order:
  1. Cross-account match  → TRANSFERS > INTERNAL_TRANSFERS
  2. Name-pattern overrides (AmEx, payroll, checks, benefit payments)
  3. Plaid confidence-based mapping (VERY_HIGH/HIGH/MEDIUM/LOW/VERY_LOW)
  4. Fallback → UNCATEGORIZED, flagged for review
"""

from datetime import datetime, timezone


# =============================================================================
# PLAID detailed category → (category, subcategory, detail, display_name,
#                             exclude_from_expenses)
# =============================================================================

PLAID_CATEGORY_MAP = {

    # -- INCOME ---------------------------------------------------------------
    'INCOME_SALARY':                              ('INCOME', 'SALARY_AND_WAGES',           'BASE_SALARY',                 'Income > Salary & Wages > Base Salary',                    True),
    'INCOME_WAGES':                               ('INCOME', 'SALARY_AND_WAGES',           'BASE_SALARY',                 'Income > Salary & Wages > Base Salary',                    True),
    'INCOME_INTEREST_EARNED':                     ('INCOME', 'INVESTMENT_INCOME',          'INTEREST',                    'Income > Investment Income > Interest',                     True),
    'INCOME_DIVIDENDS':                           ('INCOME', 'INVESTMENT_INCOME',          'DIVIDENDS',                   'Income > Investment Income > Dividends',                    True),
    'INCOME_OTHER':                               ('INCOME', 'OTHER_INCOME',               'MISCELLANEOUS',               'Income > Other Income > Miscellaneous',                     True),

    # -- EXPENSES: Housing ----------------------------------------------------
    'RENT_AND_UTILITIES_SEWAGE_AND_WASTE_MANAGEMENT': ('EXPENSES', 'HOUSING',              'UTILITIES',                   'Expenses > Housing > Utilities',                            False),
    'RENT_AND_UTILITIES_ELECTRICITY':             ('EXPENSES', 'HOUSING',                  'UTILITIES',                   'Expenses > Housing > Utilities',                            False),
    'RENT_AND_UTILITIES_GAS_AND_PROPANE':         ('EXPENSES', 'HOUSING',                  'UTILITIES',                   'Expenses > Housing > Utilities',                            False),
    'RENT_AND_UTILITIES_WATER':                   ('EXPENSES', 'HOUSING',                  'UTILITIES',                   'Expenses > Housing > Utilities',                            False),
    'RENT_AND_UTILITIES_INTERNET_AND_CABLE':      ('EXPENSES', 'HOUSING',                  'UTILITIES',                   'Expenses > Housing > Utilities',                            False),
    'RENT_AND_UTILITIES_TELEPHONE':               ('EXPENSES', 'HOUSING',                  'UTILITIES',                   'Expenses > Housing > Utilities',                            False),
    'RENT_AND_UTILITIES_OTHER_UTILITIES':         ('EXPENSES', 'HOUSING',                  'UTILITIES',                   'Expenses > Housing > Utilities',                            False),
    'RENT_AND_UTILITIES_RENT':                    ('EXPENSES', 'HOUSING',                  'RENT',                        'Expenses > Housing > Rent',                                 False),
    'HOME_IMPROVEMENT':                           ('EXPENSES', 'HOUSING',                  'HOME_MAINTENANCE',            'Expenses > Housing > Home Maintenance',                     False),
    'HOME_IMPROVEMENT_HARDWARE':                  ('EXPENSES', 'HOUSING',                  'HOME_MAINTENANCE',            'Expenses > Housing > Home Maintenance',                     False),
    'HOME_IMPROVEMENT_FURNITURE':                 ('EXPENSES', 'HOUSING',                  'HOME_MAINTENANCE',            'Expenses > Housing > Home Maintenance',                     False),
    'HOME_IMPROVEMENT_HOME_APPLIANCES':           ('EXPENSES', 'HOUSING',                  'HOME_MAINTENANCE',            'Expenses > Housing > Home Maintenance',                     False),
    'HOME_IMPROVEMENT_PAINTING':                  ('EXPENSES', 'HOUSING',                  'HOME_MAINTENANCE',            'Expenses > Housing > Home Maintenance',                     False),
    'HOME_IMPROVEMENT_SECURITY':                  ('EXPENSES', 'HOUSING',                  'HOME_MAINTENANCE',            'Expenses > Housing > Home Maintenance',                     False),
    'HOME_IMPROVEMENT_OTHER_HOME_IMPROVEMENT':    ('EXPENSES', 'HOUSING',                  'HOME_MAINTENANCE',            'Expenses > Housing > Home Maintenance',                     False),

    # -- EXPENSES: Food & Dining ----------------------------------------------
    'FOOD_AND_DRINK_GROCERIES':                   ('EXPENSES', 'FOOD_AND_DINING',          'GROCERIES',                   'Expenses > Food & Dining > Groceries',                      False),
    'FOOD_AND_DRINK_RESTAURANTS':                 ('EXPENSES', 'FOOD_AND_DINING',          'RESTAURANTS',                 'Expenses > Food & Dining > Restaurants',                    False),
    'FOOD_AND_DRINK_RESTAURANT':                  ('EXPENSES', 'FOOD_AND_DINING',          'RESTAURANTS',                 'Expenses > Food & Dining > Restaurants',                    False),  # AmEx singular variant
    'FOOD_AND_DRINK_FAST_FOOD':                   ('EXPENSES', 'FOOD_AND_DINING',          'RESTAURANTS',                 'Expenses > Food & Dining > Restaurants',                    False),
    'FOOD_AND_DRINK_COFFEE':                      ('EXPENSES', 'FOOD_AND_DINING',          'COFFEE_SHOPS',                'Expenses > Food & Dining > Coffee Shops',                   False),
    'FOOD_AND_DRINK_BEER_WINE_LIQUOR':            ('EXPENSES', 'FOOD_AND_DINING',          'ALCOHOL_BARS',                'Expenses > Food & Dining > Alcohol & Bars',                 False),
    'FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR':        ('EXPENSES', 'FOOD_AND_DINING',          'ALCOHOL_BARS',                'Expenses > Food & Dining > Alcohol & Bars',                 False),  # AmEx variant
    'FOOD_AND_DRINK_OTHER_FOOD_AND_DRINK':        ('EXPENSES', 'FOOD_AND_DINING',          'RESTAURANTS',                 'Expenses > Food & Dining > Restaurants',                    False),
    'FOOD_AND_DRINK_VENDING_MACHINES':            ('EXPENSES', 'FOOD_AND_DINING',          'RESTAURANTS',                 'Expenses > Food & Dining > Restaurants',                    False),
    'FOOD_AND_DRINK_SPECIALTY_FOODS':             ('EXPENSES', 'FOOD_AND_DINING',          'GROCERIES',                   'Expenses > Food & Dining > Groceries',                      False),

    # -- EXPENSES: Transportation ---------------------------------------------
    'TRANSPORTATION_TAXIS_AND_RIDE_SHARES':       ('EXPENSES', 'TRANSPORTATION',           'RIDE_SHARE',                  'Expenses > Transportation > Ride Share',                    False),
    'TRANSPORTATION_GAS':                         ('EXPENSES', 'TRANSPORTATION',           'GAS_FUEL',                    'Expenses > Transportation > Gas & Fuel',                    False),
    'TRANSPORTATION_PARKING':                     ('EXPENSES', 'TRANSPORTATION',           'PARKING_OUT_OF_POCKET',       'Expenses > Transportation > Parking',                       False),
    'TRANSPORTATION_TOLLS':                       ('EXPENSES', 'TRANSPORTATION',           'PARKING_OUT_OF_POCKET',       'Expenses > Transportation > Parking & Tolls',               False),
    'TRANSPORTATION_PUBLIC_TRANSIT':              ('EXPENSES', 'TRANSPORTATION',           'PUBLIC_TRANSIT_OUT_OF_POCKET','Expenses > Transportation > Public Transit',                False),
    'TRANSPORTATION_AUTOMOTIVE':                  ('EXPENSES', 'TRANSPORTATION',           'AUTO_MAINTENANCE',            'Expenses > Transportation > Auto Maintenance',              False),
    'TRANSPORTATION_OTHER_TRANSPORTATION':        ('EXPENSES', 'TRANSPORTATION',           'AUTO_MAINTENANCE',            'Expenses > Transportation > Auto Maintenance',              False),
    'TRANSPORTATION_AIRLINES':                    ('EXPENSES', 'ENTERTAINMENT_AND_TRAVEL', 'TRAVEL',                      'Expenses > Entertainment & Travel > Travel',                False),
    'TRANSPORTATION_CAR_RENTAL':                  ('EXPENSES', 'ENTERTAINMENT_AND_TRAVEL', 'TRAVEL',                      'Expenses > Entertainment & Travel > Travel',                False),
    'TRANSPORTATION_FERRIES':                     ('EXPENSES', 'TRANSPORTATION',           'PUBLIC_TRANSIT_OUT_OF_POCKET','Expenses > Transportation > Public Transit',                False),

    # -- EXPENSES: Healthcare -------------------------------------------------
    'MEDICAL_SERVICES':                           ('EXPENSES', 'HEALTHCARE',               'MEDICAL',                     'Expenses > Healthcare > Medical',                           False),
    'MEDICAL_PHARMACIES_AND_SUPPLEMENTS':         ('EXPENSES', 'HEALTHCARE',               'PRESCRIPTIONS',               'Expenses > Healthcare > Prescriptions',                     False),
    'MEDICAL_DENTAL_CARE':                        ('EXPENSES', 'HEALTHCARE',               'DENTAL',                      'Expenses > Healthcare > Dental',                            False),
    'MEDICAL_VISION_CARE':                        ('EXPENSES', 'HEALTHCARE',               'VISION',                      'Expenses > Healthcare > Vision',                            False),
    'MEDICAL_NURSING_CARE':                       ('EXPENSES', 'HEALTHCARE',               'MEDICAL',                     'Expenses > Healthcare > Medical',                           False),
    'MEDICAL_VETERINARY_SERVICES':                ('EXPENSES', 'PETS',                     'VETERINARY',                  'Expenses > Pets > Veterinary',                              False),
    'MEDICAL_PRIMARY_CARE':                       ('EXPENSES', 'HEALTHCARE',               'MEDICAL',                     'Expenses > Healthcare > Medical',                           False),
    'MEDICAL_OTHER_MEDICAL':                      ('EXPENSES', 'HEALTHCARE',               'MEDICAL',                     'Expenses > Healthcare > Medical',                           False),
    'MEDICAL_MENTAL_HEALTH':                      ('EXPENSES', 'HEALTHCARE',               'THERAPY_MENTAL_HEALTH',       'Expenses > Healthcare > Therapy / Mental Health',           False),
    'MEDICAL_HOSPITALS':                          ('EXPENSES', 'HEALTHCARE',               'MEDICAL',                     'Expenses > Healthcare > Medical',                           False),
    'MEDICAL_EMERGENCY_SERVICES':                 ('EXPENSES', 'HEALTHCARE',               'MEDICAL',                     'Expenses > Healthcare > Medical',                           False),
    'MEDICAL_IMAGING_AND_LABORATORY':             ('EXPENSES', 'HEALTHCARE',               'MEDICAL',                     'Expenses > Healthcare > Medical',                           False),
    'MEDICAL_SPECIALTY':                          ('EXPENSES', 'HEALTHCARE',               'MEDICAL',                     'Expenses > Healthcare > Medical',                           False),

    # -- EXPENSES: Personal & Fitness -----------------------------------------
    'GENERAL_SERVICES_GYMS_AND_FITNESS':          ('EXPENSES', 'PERSONAL_AND_FITNESS',     'GYM_FITNESS',                 'Expenses > Personal & Fitness > Gym & Fitness',             False),
    'PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS':     ('EXPENSES', 'PERSONAL_AND_FITNESS',     'GYM_FITNESS',                 'Expenses > Personal & Fitness > Gym & Fitness',             False),
    'GENERAL_SERVICES_PERSONAL_CARE':             ('EXPENSES', 'PERSONAL_AND_FITNESS',     'PERSONAL_CARE',               'Expenses > Personal & Fitness > Personal Care',             False),
    'PERSONAL_CARE':                              ('EXPENSES', 'PERSONAL_AND_FITNESS',     'PERSONAL_CARE',               'Expenses > Personal & Fitness > Personal Care',             False),
    'PERSONAL_CARE_HAIR_AND_BEAUTY':              ('EXPENSES', 'PERSONAL_AND_FITNESS',     'PERSONAL_CARE',               'Expenses > Personal & Fitness > Personal Care',             False),
    'PERSONAL_CARE_BATH_AND_BEAUTY':              ('EXPENSES', 'PERSONAL_AND_FITNESS',     'PERSONAL_CARE',               'Expenses > Personal & Fitness > Personal Care',             False),
    'PERSONAL_CARE_LAUNDRY_AND_DRY_CLEANING':     ('EXPENSES', 'PERSONAL_AND_FITNESS',     'PERSONAL_CARE',               'Expenses > Personal & Fitness > Personal Care',             False),
    'PERSONAL_CARE_OTHER_PERSONAL_CARE':          ('EXPENSES', 'PERSONAL_AND_FITNESS',     'PERSONAL_CARE',               'Expenses > Personal & Fitness > Personal Care',             False),
    'CLOTHING_AND_ACCESSORIES':                   ('EXPENSES', 'PERSONAL_AND_FITNESS',     'CLOTHING',                    'Expenses > Personal & Fitness > Clothing',                  False),

    # -- EXPENSES: Children ---------------------------------------------------
    'GENERAL_SERVICES_CHILDCARE':                 ('EXPENSES', 'CHILDREN',                 'CHILDCARE_OUT_OF_POCKET',     'Expenses > Children > Childcare (Out-of-Pocket)',            False),

    # -- EXPENSES: Education --------------------------------------------------
    'EDUCATION':                                  ('EXPENSES', 'EDUCATION',                'TUITION',                     'Expenses > Education > Tuition',                            False),
    'EDUCATION_COLLEGES_AND_UNIVERSITIES':        ('EXPENSES', 'EDUCATION',                'TUITION',                     'Expenses > Education > Tuition',                            False),
    'EDUCATION_ELEMENTARY_AND_SECONDARY_SCHOOLS': ('EXPENSES', 'EDUCATION',               'TUITION',                     'Expenses > Education > Tuition',                            False),
    'EDUCATION_OTHER_EDUCATION':                  ('EXPENSES', 'EDUCATION',                'TUITION',                     'Expenses > Education > Tuition',                            False),

    # -- EXPENSES: Entertainment & Travel -------------------------------------
    'ENTERTAINMENT':                              ('EXPENSES', 'ENTERTAINMENT_AND_TRAVEL', 'HOBBIES',                     'Expenses > Entertainment & Travel > Hobbies',               False),
    'ENTERTAINMENT_STREAMING_SERVICES':           ('EXPENSES', 'SUBSCRIPTION_SERVICES',    'STREAMING',                   'Expenses > Subscription Services > Streaming',              False),
    'ENTERTAINMENT_MUSIC_AND_AUDIO':              ('EXPENSES', 'SUBSCRIPTION_SERVICES',    'STREAMING',                   'Expenses > Subscription Services > Streaming',              False),
    'ENTERTAINMENT_TV_AND_MOVIES':                ('EXPENSES', 'SUBSCRIPTION_SERVICES',    'STREAMING',                   'Expenses > Subscription Services > Streaming',              False),
    'ENTERTAINMENT_GAMING':                       ('EXPENSES', 'ENTERTAINMENT_AND_TRAVEL', 'HOBBIES',                     'Expenses > Entertainment & Travel > Hobbies',               False),
    'ENTERTAINMENT_SPORTING_EVENTS_AMUSEMENT_PARKS_AND_MUSEUMS': ('EXPENSES', 'ENTERTAINMENT_AND_TRAVEL', 'EVENTS_CONCERTS', 'Expenses > Entertainment & Travel > Events & Concerts', False),
    'ENTERTAINMENT_CASINOS_AND_GAMBLING':         ('EXPENSES', 'ENTERTAINMENT_AND_TRAVEL', 'HOBBIES',                     'Expenses > Entertainment & Travel > Hobbies',               False),
    'ENTERTAINMENT_OTHER_ENTERTAINMENT':          ('EXPENSES', 'ENTERTAINMENT_AND_TRAVEL', 'HOBBIES',                     'Expenses > Entertainment & Travel > Hobbies',               False),
    'TRAVEL_FLIGHTS':                             ('EXPENSES', 'ENTERTAINMENT_AND_TRAVEL', 'TRAVEL',                      'Expenses > Entertainment & Travel > Travel',                False),
    'TRAVEL_LODGING':                             ('EXPENSES', 'ENTERTAINMENT_AND_TRAVEL', 'VACATION',                    'Expenses > Entertainment & Travel > Vacation',              False),
    'TRAVEL_VACATION_RENTALS':                    ('EXPENSES', 'ENTERTAINMENT_AND_TRAVEL', 'VACATION',                    'Expenses > Entertainment & Travel > Vacation',              False),
    'TRAVEL_CAR_RENTAL':                          ('EXPENSES', 'ENTERTAINMENT_AND_TRAVEL', 'TRAVEL',                      'Expenses > Entertainment & Travel > Travel',                False),
    'TRAVEL_OTHER_TRAVEL':                        ('EXPENSES', 'ENTERTAINMENT_AND_TRAVEL', 'TRAVEL',                      'Expenses > Entertainment & Travel > Travel',                False),

    # -- EXPENSES: General merchandise / shopping -----------------------------
    'GENERAL_MERCHANDISE':                        ('EXPENSES', 'MISCELLANEOUS',            'GENERAL_MERCHANDISE',         'Expenses > Miscellaneous > General Merchandise',            False),
    'GENERAL_MERCHANDISE_ONLINE_MARKETPLACES':    ('EXPENSES', 'MISCELLANEOUS',            'GENERAL_MERCHANDISE',         'Expenses > Miscellaneous > General Merchandise',            False),
    'GENERAL_MERCHANDISE_DISCOUNT_STORES':        ('EXPENSES', 'MISCELLANEOUS',            'GENERAL_MERCHANDISE',         'Expenses > Miscellaneous > General Merchandise',            False),
    'GENERAL_MERCHANDISE_SUPERSTORES':            ('EXPENSES', 'MISCELLANEOUS',            'GENERAL_MERCHANDISE',         'Expenses > Miscellaneous > General Merchandise',            False),
    'GENERAL_MERCHANDISE_DEPARTMENT_STORES':      ('EXPENSES', 'MISCELLANEOUS',            'GENERAL_MERCHANDISE',         'Expenses > Miscellaneous > General Merchandise',            False),
    'GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE': ('EXPENSES', 'MISCELLANEOUS',         'GENERAL_MERCHANDISE',         'Expenses > Miscellaneous > General Merchandise',            False),
    'GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES': ('EXPENSES', 'PERSONAL_AND_FITNESS',   'CLOTHING',                    'Expenses > Personal & Fitness > Clothing',                  False),
    'GENERAL_MERCHANDISE_SPORTING_GOODS':         ('EXPENSES', 'ENTERTAINMENT_AND_TRAVEL', 'HOBBIES',                     'Expenses > Entertainment & Travel > Hobbies',               False),
    'GENERAL_MERCHANDISE_PET_SUPPLIES':           ('EXPENSES', 'PETS',                     'PET_FOOD_SUPPLIES',           'Expenses > Pets > Pet Food & Supplies',                     False),
    'GENERAL_MERCHANDISE_CONVENIENCE_STORES':     ('EXPENSES', 'MISCELLANEOUS',            'GENERAL_MERCHANDISE',         'Expenses > Miscellaneous > General Merchandise',            False),
    'GENERAL_MERCHANDISE_HOME_AND_GARDEN':        ('EXPENSES', 'HOUSING',                  'HOME_MAINTENANCE',            'Expenses > Housing > Home Maintenance',                     False),
    'GENERAL_MERCHANDISE_GIFTS_AND_NOVELTIES':    ('EXPENSES', 'GIFTS_AND_DONATIONS',      'GIFTS',                       'Expenses > Gifts & Donations > Gifts',                      False),
    'GENERAL_MERCHANDISE_BOOKSTORES_AND_NEWSSTANDS': ('EXPENSES', 'EDUCATION',             'BOOKS_SUPPLIES',              'Expenses > Education > Books & Supplies',                   False),
    'GENERAL_MERCHANDISE_MUSIC_AND_INSTRUMENTS':  ('EXPENSES', 'ENTERTAINMENT_AND_TRAVEL', 'HOBBIES',                     'Expenses > Entertainment & Travel > Hobbies',               False),
    'GENERAL_MERCHANDISE_TOBACCO_AND_VAPE':       ('EXPENSES', 'MISCELLANEOUS',            'GENERAL_MERCHANDISE',         'Expenses > Miscellaneous > General Merchandise',            False),
    'GENERAL_MERCHANDISE_OFFICE_SUPPLIES':        ('EXPENSES', 'BUSINESS_EXPENSES',        'OFFICE_SUPPLIES',             'Expenses > Business Expenses > Office Supplies',            False),
    'GENERAL_MERCHANDISE_ELECTRONICS':            ('EXPENSES', 'MISCELLANEOUS',            'GENERAL_MERCHANDISE',         'Expenses > Miscellaneous > General Merchandise',            False),
    'GENERAL_MERCHANDISE_HARDWARE_STORES':        ('EXPENSES', 'HOUSING',                  'HOME_MAINTENANCE',            'Expenses > Housing > Home Maintenance',                     False),

    # -- EXPENSES: General services -------------------------------------------
    'GENERAL_SERVICES_AUTOMOTIVE':               ('EXPENSES', 'TRANSPORTATION',            'AUTO_MAINTENANCE',            'Expenses > Transportation > Auto Maintenance',              False),
    'GENERAL_SERVICES_AUTOMOTIVE_DEALERS':       ('EXPENSES', 'TRANSPORTATION',            'AUTO_MAINTENANCE',            'Expenses > Transportation > Auto Maintenance',              False),
    'GENERAL_SERVICES_INSURANCE':                ('EXPENSES', 'TRANSPORTATION',            'AUTO_INSURANCE',              'Expenses > Transportation > Auto Insurance',                False),
    'GENERAL_SERVICES_LEGAL':                    ('EXPENSES', 'FINANCIAL',                 'BANK_FEES',                   'Expenses > Financial > Legal & Professional',               False),
    'GENERAL_SERVICES_POSTAGE_AND_SHIPPING':     ('EXPENSES', 'MISCELLANEOUS',             'GENERAL_MERCHANDISE',         'Expenses > Miscellaneous > Shipping',                       False),
    'GENERAL_SERVICES_EDUCATION':                ('EXPENSES', 'EDUCATION',                 'TUITION',                     'Expenses > Education > Tuition',                            False),
    'GENERAL_SERVICES_ACCOUNTING_AND_FINANCIAL_PLANNING': ('EXPENSES', 'FINANCIAL',        'FINANCIAL_ADVISOR',           'Expenses > Financial > Financial Advisor',                  False),
    'GENERAL_SERVICES_CONSULTING_AND_LEGAL':     ('EXPENSES', 'FINANCIAL',                 'BANK_FEES',                   'Expenses > Financial > Legal & Professional',               False),
    'GENERAL_SERVICES_OTHER_GENERAL_SERVICES':   ('INCOME',   'OTHER_INCOME',              'MISCELLANEOUS',               'Income > Other Income > Miscellaneous',                     True),   # usually reimbursements

    # -- EXPENSES: Financial --------------------------------------------------
    'BANK_FEES':                                  ('EXPENSES', 'FINANCIAL',                'BANK_FEES',                   'Expenses > Financial > Bank Fees',                          False),
    'BANK_FEES_ATM_FEES':                         ('EXPENSES', 'FINANCIAL',                'BANK_FEES',                   'Expenses > Financial > Bank Fees',                          False),
    'BANK_FEES_FOREIGN_TRANSACTION':              ('EXPENSES', 'FINANCIAL',                'BANK_FEES',                   'Expenses > Financial > Bank Fees',                          False),
    'BANK_FEES_FOREIGN_TRANSACTION_FEES':         ('EXPENSES', 'FINANCIAL',                'BANK_FEES',                   'Expenses > Financial > Bank Fees',                          False),
    'BANK_FEES_OTHER_BANK_FEES':                  ('EXPENSES', 'FINANCIAL',                'BANK_FEES',                   'Expenses > Financial > Bank Fees',                          False),
    'BANK_FEES_INSUFFICIENT_FUNDS':               ('EXPENSES', 'FINANCIAL',                'BANK_FEES',                   'Expenses > Financial > Bank Fees',                          False),
    'BANK_FEES_INTEREST_CHARGE':                  ('EXPENSES', 'FINANCIAL',                'CREDIT_CARD_INTEREST_FEES',   'Expenses > Financial > Credit Card Interest & Fees',        False),

    # -- EXPENSES: Gifts & Donations ------------------------------------------
    'GOVERNMENT_AND_NON_PROFIT_DONATIONS':        ('EXPENSES', 'GIFTS_AND_DONATIONS',      'CHARITABLE_DONATIONS',        'Expenses > Gifts & Donations > Charitable Donations',       False),
    'GOVERNMENT_AND_NON_PROFIT_RELIGIOUS':        ('EXPENSES', 'GIFTS_AND_DONATIONS',      'RELIGIOUS_CONTRIBUTIONS',     'Expenses > Gifts & Donations > Religious Contributions',    False),
    'GOVERNMENT_AND_NON_PROFIT_OTHER_GOVERNMENT_AND_NON_PROFIT': ('EXPENSES', 'GIFTS_AND_DONATIONS', 'CHARITABLE_DONATIONS', 'Expenses > Gifts & Donations > Charitable Donations', False),
    'GOVERNMENT_AND_NON_PROFIT_GOVERNMENT_DEPARTMENTS_AND_AGENCIES': ('EXPENSES', 'FINANCIAL', 'BANK_FEES',               'Expenses > Financial > Government Fees',                   False),
    'GOVERNMENT_AND_NON_PROFIT_TAX_PAYMENT':      ('EXPENSES', 'FINANCIAL',                'TAX_PAYMENTS',                'Expenses > Financial > Tax Payments',                       False),

    # -- INCOME additions -----------------------------------------------------
    'INCOME_RENTAL':                              ('INCOME',   'REAL_ESTATE_INCOME',        'RENTAL_INCOME',               'Income > Real Estate Income > Rental Income',               True),
    'INCOME_TAX_REFUND':                          ('INCOME',   'OTHER_INCOME',              'TAX_REFUNDS',                 'Income > Other Income > Tax Refunds',                       True),
    'INCOME_CONTRACTOR':                          ('INCOME',   'OTHER_INCOME',              'MISCELLANEOUS',               'Income > Other Income > Miscellaneous',                     True),
    'TRANSFER_IN_OTHER_TRANSFER_IN':              ('INCOME',   'OTHER_INCOME',              'CASH_TRANSFERS_RECEIVED',     'Income > Other Income > Cash Transfers Received',           True),

    # -- ENTERTAINMENT additions ----------------------------------------------
    'ENTERTAINMENT_VIDEO_GAMES':                  ('EXPENSES', 'ENTERTAINMENT_AND_TRAVEL', 'HOBBIES',                     'Expenses > Entertainment & Travel > Hobbies',               False),

    # -- TRAVEL additions -----------------------------------------------------
    'TRAVEL_RENTAL_CARS':                         ('EXPENSES', 'ENTERTAINMENT_AND_TRAVEL', 'TRAVEL',                      'Expenses > Entertainment & Travel > Travel',                False),

    # -- HOME_IMPROVEMENT additions -------------------------------------------
    'HOME_IMPROVEMENT_REPAIR_AND_MAINTENANCE':    ('EXPENSES', 'HOUSING',                  'HOME_MAINTENANCE',            'Expenses > Housing > Home Maintenance',                     False),
    'HOME_IMPROVEMENT_POOLS_AND_OUTDOOR_AREAS':   ('EXPENSES', 'HOUSING',                  'HOME_MAINTENANCE',            'Expenses > Housing > Home Maintenance',                     False),

    # -- TRANSFERS ------------------------------------------------------------
    'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT':          ('TRANSFERS', 'CREDIT_CARD_PAYMENTS',    None,                          'Transfers > Credit Card Payments',                          True),
    'LOAN_PAYMENTS_OTHER_PAYMENT':                ('TRANSFERS', 'CREDIT_CARD_PAYMENTS',    None,                          'Transfers > Credit Card Payments',                          True),
    'LOAN_PAYMENTS_CAR_PAYMENT':                  ('EXPENSES',  'TRANSPORTATION',          'CAR_LOAN_PAYMENT',            'Expenses > Transportation > Car Loan Payment',              False),
    'LOAN_PAYMENTS_PERSONAL_LOAN_PAYMENTS':       ('EXPENSES',  'FINANCIAL',               'LOAN_PAYMENTS',               'Expenses > Financial > Loan Payments',                      False),
    'LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT':         ('EXPENSES',  'EDUCATION',               'STUDENT_LOAN_PAYMENTS',       'Expenses > Education > Student Loan Payments',              False),
    'LOAN_PAYMENTS_MORTGAGE_PAYMENT':             ('EXPENSES',  'HOUSING',                 'MORTGAGE',                    'Expenses > Housing > Mortgage',                             False),
    'LOAN_DISBURSEMENTS_PERSONAL':                ('INCOME',    'OTHER_INCOME',            'MISCELLANEOUS',               'Income > Other Income > Miscellaneous',                     True),

    # Catch-all for Plaid's OTHER_OTHER — miscellaneous items Plaid couldn't classify
    'OTHER_OTHER':                                ('EXPENSES',  'MISCELLANEOUS',           'GENERAL_MERCHANDISE',         'Expenses > Miscellaneous > Other',                          False),

    # Investment / retirement fund transfers — treated as transfers, not expenses
    'TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS': ('TRANSFERS', 'INVESTMENT_CONTRIBUTIONS', None,                       'Transfers > Investment Contributions',                      True),
    'TRANSFER_IN_INVESTMENT_AND_RETIREMENT_FUNDS':  ('TRANSFERS', 'INTERNAL_TRANSFERS',    None,                          'Transfers > Internal Transfers',                            True),

    # Savings account transfers
    'TRANSFER_OUT_SAVINGS':                       ('TRANSFERS', 'INTERNAL_TRANSFERS',      None,                          'Transfers > Internal Transfers',                            True),
    'TRANSFER_IN_SAVINGS':                        ('TRANSFERS', 'INTERNAL_TRANSFERS',      None,                          'Transfers > Internal Transfers',                            True),

    # Cash withdrawals
    'TRANSFER_OUT_WITHDRAWAL':                    ('EXPENSES',  'FINANCIAL',               'CASH_TRANSFERS_SENT',         'Expenses > Financial > Cash Withdrawals',                   False),

    # App transfers — default if cross-account match not found
    'TRANSFER_OUT_TRANSFER_OUT_FROM_APPS':        ('EXPENSES',  'FINANCIAL',               'CASH_TRANSFERS_SENT',         'Expenses > Financial > Cash Transfers Sent',                False),
    'TRANSFER_IN_TRANSFER_IN_FROM_APPS':          ('INCOME',    'OTHER_INCOME',            'CASH_TRANSFERS_RECEIVED',     'Income > Other Income > Cash Transfers Received',           True),

    # Account transfers — resolved via cross-account matching; None = flag if unmatched
    'TRANSFER_OUT_ACCOUNT_TRANSFER':              None,
    'TRANSFER_IN_ACCOUNT_TRANSFER':               None,
}

# Categories that benefit from cross-account check before mapping
NEEDS_CROSS_MATCH = {
    'TRANSFER_OUT_ACCOUNT_TRANSFER',
    'TRANSFER_IN_ACCOUNT_TRANSFER',
    'TRANSFER_OUT_TRANSFER_OUT_FROM_APPS',
    'TRANSFER_IN_TRANSFER_IN_FROM_APPS',
}

RENT_UTILITIES_PREFIX = 'RENT_AND_UTILITIES_'


# =============================================================================
# CROSS-ACCOUNT MATCHING
# =============================================================================

def find_cross_account_matches(transactions: list[dict]) -> dict:
    """
    Find transaction pairs across different accounts representing internal
    transfers: same |amount|, opposite signs, ≤1 day apart, different accounts.

    Input:  list of transaction dicts with keys:
              plaid_transaction_id, plaid_account_id, date (date obj or str), amount
    Returns: dict mapping plaid_transaction_id → matched plaid_transaction_id
    """
    from datetime import date as date_type

    flat = []
    for txn in transactions:
        d = txn['date']
        if isinstance(d, str):
            d = date_type.fromisoformat(d)
        flat.append({
            'id':     txn['plaid_transaction_id'],
            'acct':   txn['account_id'],  # Supabase UUID — uniquely identifies each account
            'date':   d,
            'amount': float(txn['amount']),
        })

    matches = {}
    matched = set()

    for i, a in enumerate(flat):
        if a['id'] in matched:
            continue
        for j, b in enumerate(flat):
            if j <= i or b['id'] in matched:
                continue
            if a['acct'] == b['acct']:
                continue
            if abs(abs(a['amount']) - abs(b['amount'])) > 0.01:
                continue
            if not ((a['amount'] > 0 and b['amount'] < 0) or
                    (a['amount'] < 0 and b['amount'] > 0)):
                continue
            if abs((a['date'] - b['date']).days) > 1:
                continue
            matches[a['id']] = b['id']
            matches[b['id']] = a['id']
            matched.update({a['id'], b['id']})
            break

    return matches


# =============================================================================
# PER-TRANSACTION CATEGORIZATION
# =============================================================================

def categorize_transaction(txn: dict, cross_matches: dict) -> dict:
    """
    Categorize a single transaction.

    Input txn dict must have:
      plaid_transaction_id, name, amount, check_number (optional),
      plaid_category_detailed, plaid_category_confidence

    Returns a dict of fields to merge into the transaction row:
      category, subcategory, detail, category_display_name,
      exclude_from_expenses, categorization_source,
      categorization_confidence, flagged_for_review, flag_reason, notes,
      categorized_at
    """
    now_str = datetime.now(timezone.utc).isoformat()

    tid      = txn['plaid_transaction_id']
    name     = txn.get('name', '') or ''
    amount   = float(txn.get('amount', 0))
    detailed = txn.get('plaid_category_detailed', '') or ''
    conf     = txn.get('plaid_category_confidence', '') or ''

    def result(cat, subcat, detail, display, exclude, source, confidence, flagged, reason, notes=None):
        return {
            'category':                 cat,
            'subcategory':              subcat,
            'detail':                   detail,
            'category_display_name':    display,
            'exclude_from_expenses':    exclude,
            'categorization_source':    source,
            'categorization_confidence':confidence,
            'flagged_for_review':       flagged,
            'flag_reason':              reason,
            'notes':                    notes,
            'categorized_at':           now_str,
        }

    name_upper = name.upper()

    # -------------------------------------------------------------------------
    # Step 1a: CC payment name patterns — must run BEFORE cross-account match
    # because CC payments to connected cards appear as cross-account pairs and
    # would otherwise be mis-categorized as INTERNAL_TRANSFERS.
    # -------------------------------------------------------------------------
    _CC_PATTERNS = (
        'AMEX EPAYMENT',              # AmEx online CC bill payment (AMEX EPAYMENT ACH PMT)
        'CHASE CREDIT CRD',           # Chase credit card autopay
        'CREDIT CRD AUTOPAY',         # generic CC autopay
        'CITI AUTOPAY',               # Citi autopay
        'CITICARDS',                  # Citi CC portal
        'CREDIT CARD PAYMENT',        # generic
        'AUTOMATIC PAYMENT - THANK',  # Chase CC account-side autopayment credit.
                                      # Plaid occasionally miscategorizes this as INCOME_SALARY
                                      # after a Chase OAuth re-auth. The pattern match in Step 1a
                                      # fires before Plaid category lookup, making it immune to
                                      # that re-categorization bug.
        # NOTE: 'AMERICANEXPRESS' intentionally omitted — "AMERICANEXPRESS TRANSFER"
        # is a savings/HYSA transfer, not a CC payment. AMEX EPAYMENT covers CC bills.
    )
    for _pat in _CC_PATTERNS:
        if _pat in name_upper:
            return result(
                'TRANSFERS', 'CREDIT_CARD_PAYMENTS', None,
                'Transfers > Credit Card Payments', True,
                'name_pattern_override', 'HIGH', False, None,
                f"CC payment matched on '{_pat}'.",
            )

    # -------------------------------------------------------------------------
    # Step 1b: Cross-account match → internal transfer
    # Skip if the Plaid detailed category is clearly a retail/consumer expense —
    # those are never genuine internal transfers even if amounts coincidentally match.
    # -------------------------------------------------------------------------
    _EXPENSE_PLAID_PREFIXES = (
        'FOOD_AND_DRINK_',
        'ENTERTAINMENT_',
        'SHOPPING_',
        'TRAVEL_',
        'PERSONAL_CARE_',
        'GENERAL_SERVICES_',
        'MEDICAL_',
        'GOVERNMENT_AND_NON_PROFIT_',
        'HOME_IMPROVEMENT_',
        'RENT_AND_UTILITIES_',
        'GENERAL_MERCHANDISE_',
    )
    if tid in cross_matches:
        is_clearly_expense = any(detailed.startswith(pfx) for pfx in _EXPENSE_PLAID_PREFIXES)
        if not is_clearly_expense:
            matched_id = cross_matches[tid]
            return result(
                'TRANSFERS', 'INTERNAL_TRANSFERS', None,
                'Transfers > Internal Transfers', True,
                'cross_account_match', 'HIGH', False, None,
                f"Matched with transaction {matched_id} (${abs(amount):,.2f})",
            )
        # Otherwise fall through — Plaid mapping will categorize it correctly

    # -------------------------------------------------------------------------
    # Step 2a: AmEx name overrides — handles both HYSA transfers and CC payments.
    # Must run before Step 3 (Plaid map) because Plaid labels HYSA transfers
    # as LOAN_PAYMENTS_CREDIT_CARD_PAYMENT with HIGH confidence, which would
    # otherwise route them to CREDIT_CARD_PAYMENTS.
    # -------------------------------------------------------------------------
    if 'AMERICANEXPRESS' in name_upper:
        if 'TRANSFER' in name_upper:
            # Savings/HYSA transfer (e.g. "AMERICANEXPRESS TRANSFER 320026575270")
            # Always INTERNAL_TRANSFERS regardless of what Plaid says.
            return result(
                'TRANSFERS', 'INTERNAL_TRANSFERS', None,
                'Transfers > Internal Transfers', True,
                'name_pattern_override', 'HIGH', False, None,
                'AmEx savings/HYSA transfer — routed to internal transfers.',
            )
        else:
            # Ambiguous AmEx — best guess is CC payment, flag for review
            return result(
                'TRANSFERS', 'CREDIT_CARD_PAYMENTS', None,
                'Transfers > Credit Card Payments', True,
                'name_pattern_override', 'NEEDS_REVIEW', True,
                'AmEx transaction requires manual review',
                'Best guess: credit card payment. Confirm when AmEx account is connected.',
            )

    # -------------------------------------------------------------------------
    # Step 2b: Known payroll patterns (extend this list as needed)
    # -------------------------------------------------------------------------
    for payroll_keyword in ('PARAMOUNT GLOBAL PAYROLL', 'MERLYN MIND'):
        if payroll_keyword in name_upper:
            return result(
                'INCOME', 'SALARY_AND_WAGES', 'BASE_SALARY',
                'Income > Salary & Wages > Base Salary', True,
                'name_pattern_override', 'HIGH', False, None,
                f"Payroll direct deposit matched on '{payroll_keyword}'.",
            )

    # -------------------------------------------------------------------------
    # Step 2c: Physical checks
    # -------------------------------------------------------------------------
    if txn.get('check_number') or name_upper.startswith('CHECK '):
        return result(
            'EXPENSES', 'CHECKING_RELATED_EXPENSES', 'CHECK_PAYMENTS',
            'Expenses > Checking Related Expenses > Check Payments', False,
            'name_pattern_override', 'LOW', True,
            'Physical check — payee and purpose unknown',
            'Look for recurring patterns to identify payee. Override once confirmed.',
        )

    # -------------------------------------------------------------------------
    # Step 2d: Benefit payments — truly ambiguous
    # -------------------------------------------------------------------------
    if 'BENEFIT PAYMENT' in name_upper:
        return result(
            None, None, None, 'Uncategorized', False,
            'name_pattern_override', 'UNCATEGORIZED', True,
            'Ambiguous benefit payment — insufficient information',
            'Could be insurance reimbursement, employer benefit, FSA payout, etc. Needs manual categorization.',
        )

    # -------------------------------------------------------------------------
    # Step 2e: Known software/subscription vendors (Plaid misfires on these)
    # Extend this list as new mis-classified vendors are found.
    # -------------------------------------------------------------------------
    for vendor in ('ANTHROPIC', 'OPENAI', 'FIGMA', 'SUPABASE', 'GOOGLE ONE', 'VERCEL'):
        if vendor in name_upper:
            return result(
                'EXPENSES', 'SUBSCRIPTION_SERVICES', 'SOFTWARE_TOOLS',
                'Expenses > Subscription Services > Software & Tools', False,
                'name_pattern_override', 'HIGH', False, None,
                f"Known software vendor matched on '{vendor}'.",
            )

    if 'AMAZON PRIME' in name_upper:
        return result(
            'EXPENSES', 'SUBSCRIPTION_SERVICES', 'STREAMING',
            'Expenses > Subscription Services > Streaming', False,
            'name_pattern_override', 'HIGH', False, None,
            "Amazon Prime subscription matched on 'AMAZON PRIME'.",
        )

    # -------------------------------------------------------------------------
    # Step 3: Plaid confidence-based mapping
    # -------------------------------------------------------------------------

    def map_from_plaid(flag, confidence_label, flag_reason_prefix):
        mapping = PLAID_CATEGORY_MAP.get(detailed)
        if not mapping and detailed.startswith(RENT_UTILITIES_PREFIX):
            mapping = ('EXPENSES', 'HOUSING', 'UTILITIES', 'Expenses > Housing > Utilities', False)
        if mapping:
            cat, subcat, det, display, exclude = mapping
            # If Plaid says INCOME but the transaction is a debit (amount > 0),
            # that's a logical contradiction — re-route to miscellaneous expenses.
            if cat == 'INCOME' and amount > 0:
                return result(
                    'EXPENSES', 'MISCELLANEOUS', None,
                    'Expenses > Miscellaneous', False,
                    'automated_plaid_mapping', 'LOW', True,
                    "Plaid classified as income but transaction is a charge — re-routed to miscellaneous expenses for review",
                    f"Plaid category '{detailed}' (confidence: {conf}) — overridden because amount > 0 (debit).",
                )
            note = f"Plaid category '{detailed}' (confidence: {conf})." if flag else None
            return result(cat, subcat, det, display, exclude,
                          'automated_plaid_mapping', confidence_label,
                          flag, f"{flag_reason_prefix} ({conf})" if flag else None, note)
        return result(None, None, None, 'Uncategorized', False,
                      'automated_plaid_mapping', 'UNCATEGORIZED', True,
                      f"No mapping for Plaid category '{detailed}'",
                      f"Plaid category '{detailed}' (confidence: {conf}) has no mapping.")

    if conf in ('LOW', 'VERY_LOW', ''):
        return map_from_plaid(True, 'LOW', 'Low Plaid confidence — needs confirmation')

    if conf == 'MEDIUM':
        return map_from_plaid(True, 'MEDIUM', 'Medium Plaid confidence — spot-check recommended')

    if conf in ('VERY_HIGH', 'HIGH'):
        if detailed in NEEDS_CROSS_MATCH:
            if detailed in ('TRANSFER_OUT_TRANSFER_OUT_FROM_APPS', 'TRANSFER_IN_TRANSFER_IN_FROM_APPS'):
                mapping = PLAID_CATEGORY_MAP[detailed]
                cat, subcat, det, display, exclude = mapping
                return result(cat, subcat, det, display, exclude,
                              'automated_plaid_mapping', 'HIGH', True,
                              'P2P transfer — verify not an internal transfer or reimbursement',
                              'No matching transaction found in other accounts. Confirm counterparty context.')
            return result(None, None, None, 'Uncategorized', False,
                          'automated_plaid_mapping', 'NEEDS_REVIEW', True,
                          'Account transfer with no cross-account match found',
                          f"Plaid: '{detailed}'. No matching transaction in other accounts — needs manual categorization.")
        return map_from_plaid(False, 'HIGH', '')

    # -------------------------------------------------------------------------
    # Step 4: Fallback
    # -------------------------------------------------------------------------
    return result(None, None, None, 'Uncategorized', False,
                  'automated_plaid_mapping', 'UNCATEGORIZED', True,
                  'No categorization rule matched',
                  f"Plaid detailed='{detailed}', confidence='{conf}' — needs manual categorization.")
