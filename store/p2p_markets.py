# store/p2p_markets.py
"""
P2P currencies and payment methods (Paxful-style coverage).

  CURRENCIES  code → (name, flag, region, [local method keys])
  METHODS     key  → (display name, what to ask for: phone / account / email…)
  UNIVERSAL   methods offered in every currency (bank transfer, cash, Wise, PayPal…)

Left out on purpose:
  • gift cards: the main fraud and money-laundering channel on P2P sites;
  • currencies under comprehensive sanctions (IRR, KPW, SYP, CUP) and RUB / BYN (sanctions risk for the site
    and its banking partners). Add them back only after legal advice.
Existing method keys (mpesa_ke, bank_ke, …) are kept so saved payment methods and ads stay valid.
"""


def _flag(cc):
    return ''.join(chr(0x1F1E6 + ord(c) - 65) for c in cc.upper()) if len(cc) == 2 else '🌍'


PHONE, ACCOUNT, EMAIL, ID = 'Phone number', 'Account number / IBAN', 'Email or username', 'Account ID / tag'

METHODS = {
    # ── universal (every currency) ──
    'bank_transfer': ('Bank transfer', ACCOUNT), 'cash_in_person': ('Cash in person', 'Meeting place / phone'),
    'cash_deposit': ('Cash deposit at bank', ACCOUNT), 'wise': ('Wise', 'Email or Wise tag'), 'paypal': ('PayPal', EMAIL),
    'skrill': ('Skrill', 'Email'), 'neteller': ('Neteller', 'Email'), 'payoneer': ('Payoneer', 'Email'),
    'revolut': ('Revolut', 'Revtag or phone'), 'western_union': ('Western Union', 'Full name + country'),
    'moneygram': ('MoneyGram', 'Full name + country'), 'worldremit': ('WorldRemit', 'Full name + phone'),
    # ── Kenya / East Africa (keys kept from the first version) ──
    'mpesa_ke': ('M-Pesa', PHONE), 'airtel_ke': ('Airtel Money', PHONE), 'bank_ke': ('Bank transfer (Kenya)', ACCOUNT),
    'cash_ke': ('M-Pesa Paybill / Till', 'Paybill or till number'), 'tkash_ke': ('T-Kash', PHONE),
    'mtn_ug': ('MTN Mobile Money', PHONE), 'airtel_ug': ('Airtel Money', PHONE), 'bank_ug': ('Bank transfer (Uganda)', ACCOUNT),
    'mpesa_tz': ('Vodacom M-Pesa', PHONE), 'tigo_tz': ('Mixx by Yas (Tigo Pesa)', PHONE), 'airtel_tz': ('Airtel Money', PHONE),
    'halopesa_tz': ('HaloPesa', PHONE), 'bank_tz': ('Bank transfer (Tanzania)', ACCOUNT),
    'mtn_rw': ('MTN MoMo', PHONE), 'airtel_rw': ('Airtel Money', PHONE), 'bank_rw': ('Bank transfer (Rwanda)', ACCOUNT),
    'lumicash_bi': ('Lumicash', PHONE), 'ecocash_bi': ('EcoCash', PHONE),
    'telebirr_et': ('telebirr', PHONE), 'cbebirr_et': ('CBE Birr', PHONE), 'mpesa_et': ('M-Pesa Ethiopia', PHONE),
    'evcplus_so': ('EVC Plus', PHONE), 'zaad_so': ('ZAAD', PHONE), 'edahab_so': ('eDahab', PHONE),
    'mgurush_ss': ('m-Gurush', PHONE), 'bankak_sd': ('Bankak', ACCOUNT), 'waafi_dj': ('WAAFI', PHONE),
    # ── West Africa ──
    'bank_ng': ('Bank transfer (Nigeria)', ACCOUNT), 'opay_ng': ('OPay', 'OPay number'), 'palmpay_ng': ('PalmPay', 'PalmPay number'),
    'kuda_ng': ('Kuda', ACCOUNT), 'moniepoint_ng': ('Moniepoint', ACCOUNT),
    'mtn_gh': ('MTN MoMo', PHONE), 'telecel_gh': ('Telecel Cash', PHONE), 'at_gh': ('AT Money', PHONE), 'bank_gh': ('Bank transfer (Ghana)', ACCOUNT),
    'orange_money': ('Orange Money', PHONE), 'wave': ('Wave', PHONE), 'mtn_momo': ('MTN Mobile Money', PHONE),
    'moov_money': ('Moov Money', PHONE), 'airtel_money': ('Airtel Money', PHONE), 'free_money_sn': ('Free Money', PHONE),
    'afrimoney': ('Afrimoney', PHONE), 'qmoney_gm': ('QMoney', PHONE), 'lonestar_lr': ('Lonestar MTN MoMo', PHONE),
    # ── Central & Southern Africa ──
    'mpesa_cd': ('M-Pesa RDC', PHONE), 'mpesa_mz': ('M-Pesa Moçambique', PHONE), 'emola_mz': ('e-Mola', PHONE),
    'mtn_zm': ('MTN MoMo', PHONE), 'airtel_zm': ('Airtel Money', PHONE), 'zamtel_zm': ('Zamtel Kwacha', PHONE),
    'airtel_mw': ('Airtel Money', PHONE), 'mpamba_mw': ('TNM Mpamba', PHONE),
    'ecocash_zw': ('EcoCash', PHONE), 'onemoney_zw': ('OneMoney', PHONE), 'innbucks_zw': ('InnBucks', PHONE),
    'bank_za': ('Bank transfer (EFT)', ACCOUNT), 'capitec_za': ('Capitec Pay', 'Phone or account number'),
    'payshap_za': ('PayShap', 'ShapID or phone'), 'fnb_ewallet_za': ('FNB eWallet', PHONE),
    'orange_bw': ('Orange Money', PHONE), 'myzaka_bw': ('MyZaka', PHONE), 'ewallet_na': ('FNB / Bank Windhoek eWallet', PHONE),
    'mpesa_ls': ('M-Pesa Lesotho', PHONE), 'ecocash_ls': ('EcoCash', PHONE), 'mtn_sz': ('MTN MoMo', PHONE),
    'unitel_ao': ('Unitel Money', PHONE), 'multicaixa_ao': ('Multicaixa Express', PHONE),
    'mvola_mg': ('MVola', PHONE), 'orange_mg': ('Orange Money', PHONE), 'juice_mu': ('MCB Juice', PHONE),
    'mytmoney_mu': ('my.t money', PHONE),
    # ── North Africa ──
    'vodafone_cash_eg': ('Vodafone Cash', PHONE), 'instapay_eg': ('InstaPay', 'IPA address or phone'), 'fawry_eg': ('Fawry', PHONE),
    'cih_ma': ('CIH Bank', ACCOUNT), 'baridimob_dz': ('BaridiMob / CCP', 'CCP number'), 'd17_tn': ('D17', PHONE),
    'bankily_mr': ('Bankily', PHONE),
    # ── Asia ──
    'upi_in': ('UPI', 'UPI ID'), 'imps_in': ('IMPS / NEFT', 'Account + IFSC'), 'paytm_in': ('Paytm', PHONE),
    'phonepe_in': ('PhonePe', 'UPI ID'), 'gpay_in': ('Google Pay (India)', 'UPI ID'),
    'jazzcash_pk': ('JazzCash', PHONE), 'easypaisa_pk': ('Easypaisa', PHONE), 'raast_pk': ('Raast', 'Raast ID / IBAN'),
    'bkash_bd': ('bKash', PHONE), 'nagad_bd': ('Nagad', PHONE), 'rocket_bd': ('Rocket', PHONE),
    'ezcash_lk': ('eZ Cash', PHONE), 'esewa_np': ('eSewa', PHONE), 'khalti_np': ('Khalti', PHONE),
    'alipay_cn': ('Alipay', 'Alipay account'), 'wechat_cn': ('WeChat Pay', 'WeChat ID'), 'fps_hk': ('FPS', 'FPS ID or phone'),
    'payme_hk': ('PayMe', PHONE), 'jkopay_tw': ('JKOPAY', PHONE), 'paypay_jp': ('PayPay', PHONE),
    'kakaopay_kr': ('KakaoPay', PHONE), 'toss_kr': ('Toss', PHONE),
    'gcash_ph': ('GCash', PHONE), 'maya_ph': ('Maya', PHONE), 'instapay_ph': ('InstaPay (PH)', ACCOUNT),
    'dana_id': ('DANA', PHONE), 'ovo_id': ('OVO', PHONE), 'gopay_id': ('GoPay', PHONE),
    'duitnow_my': ('DuitNow', 'DuitNow ID'), 'tng_my': ("Touch 'n Go eWallet", PHONE), 'paynow_sg': ('PayNow', 'UEN or phone'),
    'promptpay_th': ('PromptPay', 'PromptPay ID'), 'truemoney_th': ('TrueMoney', PHONE),
    'momo_vn': ('MoMo', PHONE), 'zalopay_vn': ('ZaloPay', PHONE), 'aba_kh': ('ABA Pay', ACCOUNT), 'wing_kh': ('Wing', PHONE),
    'bcel_la': ('BCEL One', ACCOUNT), 'kbzpay_mm': ('KBZPay', PHONE), 'wavepay_mm': ('WavePay', PHONE),
    'qpay_mn': ('QPay', PHONE), 'kaspi_kz': ('Kaspi', PHONE), 'click_uz': ('Click', PHONE), 'payme_uz': ('Payme', PHONE),
    'mbank_kg': ('MBank', PHONE), 'alif_tj': ('Alif Mobi', PHONE), 'm10_az': ('m10', PHONE), 'tbc_ge': ('TBC Pay', ACCOUNT),
    'idram_am': ('Idram', PHONE), 'hesabpay_af': ('HesabPay', PHONE),
    # ── Middle East ──
    'stcpay_sa': ('STC Pay', PHONE), 'urpay_sa': ('urpay', PHONE), 'payit_ae': ('payit', PHONE), 'benefitpay_bh': ('BenefitPay', PHONE),
    'cliq_jo': ('CliQ', 'CliQ alias'), 'bit_il': ('Bit', PHONE), 'paybox_il': ('PayBox', PHONE),
    'papara_tr': ('Papara', 'Papara number'), 'havale_tr': ('Havale / FAST (IBAN)', ACCOUNT),
    'whish_lb': ('Whish Money', PHONE), 'omt_lb': ('OMT', 'Full name + phone'), 'zaincash_iq': ('ZainCash', PHONE),
    'fastpay_iq': ('FastPay', PHONE), 'jaib_ye': ('Jaib', PHONE),
    # ── Europe ──
    'sepa_eu': ('SEPA transfer', 'IBAN'), 'sepa_instant_eu': ('SEPA Instant', 'IBAN'), 'bizum_es': ('Bizum', PHONE),
    'satispay_it': ('Satispay', PHONE), 'lydia_fr': ('Lydia / Sumeria', PHONE), 'ideal_nl': ('iDEAL / Tikkie', 'IBAN'),
    'mbway_pt': ('MB WAY', PHONE), 'paylib_fr': ('Wero / Paylib', PHONE), 'payconiq_be': ('Payconiq', PHONE),
    'faster_payments_gb': ('Faster Payments', 'Sort code + account'), 'monzo_gb': ('Monzo', 'Monzo.me link or phone'),
    'twint_ch': ('TWINT', PHONE), 'swish_se': ('Swish', PHONE), 'vipps_no': ('Vipps MobilePay', PHONE),
    'mobilepay_dk': ('MobilePay', PHONE), 'blik_pl': ('BLIK (phone)', PHONE), 'qr_cz': ('Bank QR / account', ACCOUNT),
    'privatbank_ua': ('PrivatBank', 'Card number'), 'monobank_ua': ('monobank', 'Card number'),
    'revolut_ro': ('Revolut RO / bank', ACCOUNT), 'mia_md': ('MIA instant', PHONE),
    # ── Americas ──
    'bank_us': ('Bank transfer (ACH)', 'Routing + account'), 'zelle_us': ('Zelle', 'Email or phone'), 'cashapp_us': ('Cash App', '$Cashtag'),
    'venmo_us': ('Venmo', '@username'), 'chime_us': ('Chime', '$ChimeSign'), 'apple_cash_us': ('Apple Cash', PHONE),
    'interac_ca': ('Interac e-Transfer', 'Email or phone'), 'spei_mx': ('SPEI', 'CLABE'), 'oxxo_mx': ('OXXO deposit', 'Card number'),
    'mercadopago': ('Mercado Pago', 'CVU / alias / email'), 'pix_br': ('Pix', 'Pix key'), 'cvu_ar': ('CVU / alias (bank)', 'CVU or alias'),
    'uala_ar': ('Ualá', 'CVU or alias'), 'brubank_ar': ('Brubank', 'CVU or alias'),
    'nequi_co': ('Nequi', PHONE), 'daviplata_co': ('Daviplata', PHONE), 'bancolombia_co': ('Bancolombia', ACCOUNT),
    'yape_pe': ('Yape', PHONE), 'plin_pe': ('Plin', PHONE), 'pagomovil_ve': ('Pago Móvil', 'Phone + bank + ID'),
    'zinli_ve': ('Zinli', 'Email'), 'mach_cl': ('MACH', PHONE), 'transfer_cl': ('Transferencia (Chile)', 'RUT + account'),
    'tigomoney_bo': ('Tigo Money', PHONE), 'tigomoney_py': ('Tigo Money', PHONE), 'prex_uy': ('Prex', ACCOUNT),
    'sinpe_cr': ('SINPE Móvil', PHONE), 'tigomoney_hn': ('Tigo Money', PHONE), 'tigomoney_gt': ('Tigo Money', PHONE),
    'yappy_pa': ('Yappy', PHONE), 'tpago_do': ('tPago', PHONE), 'moncash_ht': ('MonCash', PHONE), 'natcash_ht': ('NatCash', PHONE),
    'lynk_jm': ('Lynk', PHONE), 'linx_tt': ('Linx / bank', ACCOUNT),
    # ── Oceania ──
    'payid_au': ('PayID / Osko', 'PayID (email or phone)'), 'bank_nz': ('Bank transfer (NZ)', ACCOUNT),
    'mpaisa_fj': ('M-PAiSA', PHONE), 'mycash_fj': ('MyCash', PHONE), 'cellmoni_pg': ('CellMoni', PHONE),
}

UNIVERSAL = ['bank_transfer', 'cash_in_person', 'cash_deposit', 'wise', 'paypal', 'skrill', 'neteller', 'payoneer',
             'revolut', 'western_union', 'moneygram', 'worldremit']

# code: (name, country code for the flag, region, local methods)
_C = [
    # Africa
    ('KES', 'Kenyan shilling', 'KE', 'Africa', ['mpesa_ke', 'airtel_ke', 'bank_ke', 'cash_ke', 'tkash_ke']),
    ('UGX', 'Ugandan shilling', 'UG', 'Africa', ['mtn_ug', 'airtel_ug', 'bank_ug']),
    ('TZS', 'Tanzanian shilling', 'TZ', 'Africa', ['mpesa_tz', 'tigo_tz', 'airtel_tz', 'halopesa_tz', 'bank_tz']),
    ('RWF', 'Rwandan franc', 'RW', 'Africa', ['mtn_rw', 'airtel_rw', 'bank_rw']),
    ('BIF', 'Burundian franc', 'BI', 'Africa', ['lumicash_bi', 'ecocash_bi']),
    ('ETB', 'Ethiopian birr', 'ET', 'Africa', ['telebirr_et', 'cbebirr_et', 'mpesa_et']),
    ('SOS', 'Somali shilling', 'SO', 'Africa', ['evcplus_so', 'zaad_so', 'edahab_so']),
    ('SSP', 'South Sudanese pound', 'SS', 'Africa', ['mgurush_ss']),
    ('SDG', 'Sudanese pound', 'SD', 'Africa', ['bankak_sd']),
    ('DJF', 'Djiboutian franc', 'DJ', 'Africa', ['waafi_dj']),
    ('ERN', 'Eritrean nakfa', 'ER', 'Africa', []),
    ('NGN', 'Nigerian naira', 'NG', 'Africa', ['bank_ng', 'opay_ng', 'palmpay_ng', 'kuda_ng', 'moniepoint_ng']),
    ('GHS', 'Ghanaian cedi', 'GH', 'Africa', ['mtn_gh', 'telecel_gh', 'at_gh', 'bank_gh']),
    ('XOF', 'West African CFA franc', 'SN', 'Africa', ['orange_money', 'wave', 'mtn_momo', 'moov_money', 'free_money_sn']),
    ('XAF', 'Central African CFA franc', 'CM', 'Africa', ['mtn_momo', 'orange_money', 'airtel_money', 'moov_money']),
    ('GMD', 'Gambian dalasi', 'GM', 'Africa', ['wave', 'afrimoney', 'qmoney_gm']),
    ('GNF', 'Guinean franc', 'GN', 'Africa', ['orange_money', 'mtn_momo']),
    ('SLE', 'Sierra Leonean leone', 'SL', 'Africa', ['orange_money', 'afrimoney']),
    ('LRD', 'Liberian dollar', 'LR', 'Africa', ['lonestar_lr', 'orange_money']),
    ('CVE', 'Cape Verdean escudo', 'CV', 'Africa', []),
    ('MRU', 'Mauritanian ouguiya', 'MR', 'Africa', ['bankily_mr']),
    ('CDF', 'Congolese franc', 'CD', 'Africa', ['mpesa_cd', 'orange_money', 'airtel_money']),
    ('AOA', 'Angolan kwanza', 'AO', 'Africa', ['unitel_ao', 'multicaixa_ao']),
    ('ZMW', 'Zambian kwacha', 'ZM', 'Africa', ['mtn_zm', 'airtel_zm', 'zamtel_zm']),
    ('MWK', 'Malawian kwacha', 'MW', 'Africa', ['airtel_mw', 'mpamba_mw']),
    ('MZN', 'Mozambican metical', 'MZ', 'Africa', ['mpesa_mz', 'emola_mz']),
    ('ZWG', 'Zimbabwe gold (ZiG)', 'ZW', 'Africa', ['ecocash_zw', 'onemoney_zw', 'innbucks_zw']),
    ('ZAR', 'South African rand', 'ZA', 'Africa', ['bank_za', 'capitec_za', 'payshap_za', 'fnb_ewallet_za']),
    ('BWP', 'Botswana pula', 'BW', 'Africa', ['orange_bw', 'myzaka_bw']),
    ('NAD', 'Namibian dollar', 'NA', 'Africa', ['ewallet_na']),
    ('LSL', 'Lesotho loti', 'LS', 'Africa', ['mpesa_ls', 'ecocash_ls']),
    ('SZL', 'Eswatini lilangeni', 'SZ', 'Africa', ['mtn_sz']),
    ('MGA', 'Malagasy ariary', 'MG', 'Africa', ['mvola_mg', 'orange_mg']),
    ('MUR', 'Mauritian rupee', 'MU', 'Africa', ['juice_mu', 'mytmoney_mu']),
    ('SCR', 'Seychellois rupee', 'SC', 'Africa', []),
    ('KMF', 'Comorian franc', 'KM', 'Africa', []),
    ('STN', 'São Tomé dobra', 'ST', 'Africa', []),
    ('EGP', 'Egyptian pound', 'EG', 'Africa', ['vodafone_cash_eg', 'instapay_eg', 'fawry_eg']),
    ('MAD', 'Moroccan dirham', 'MA', 'Africa', ['cih_ma']),
    ('DZD', 'Algerian dinar', 'DZ', 'Africa', ['baridimob_dz']),
    ('TND', 'Tunisian dinar', 'TN', 'Africa', ['d17_tn']),
    ('LYD', 'Libyan dinar', 'LY', 'Africa', []),
    # Asia
    ('INR', 'Indian rupee', 'IN', 'Asia', ['upi_in', 'imps_in', 'paytm_in', 'phonepe_in', 'gpay_in']),
    ('PKR', 'Pakistani rupee', 'PK', 'Asia', ['jazzcash_pk', 'easypaisa_pk', 'raast_pk']),
    ('BDT', 'Bangladeshi taka', 'BD', 'Asia', ['bkash_bd', 'nagad_bd', 'rocket_bd']),
    ('LKR', 'Sri Lankan rupee', 'LK', 'Asia', ['ezcash_lk']),
    ('NPR', 'Nepalese rupee', 'NP', 'Asia', ['esewa_np', 'khalti_np']),
    ('AFN', 'Afghan afghani', 'AF', 'Asia', ['hesabpay_af']),
    ('CNY', 'Chinese yuan', 'CN', 'Asia', ['alipay_cn', 'wechat_cn']),
    ('HKD', 'Hong Kong dollar', 'HK', 'Asia', ['fps_hk', 'payme_hk']),
    ('TWD', 'New Taiwan dollar', 'TW', 'Asia', ['jkopay_tw']),
    ('JPY', 'Japanese yen', 'JP', 'Asia', ['paypay_jp']),
    ('KRW', 'South Korean won', 'KR', 'Asia', ['kakaopay_kr', 'toss_kr']),
    ('PHP', 'Philippine peso', 'PH', 'Asia', ['gcash_ph', 'maya_ph', 'instapay_ph']),
    ('IDR', 'Indonesian rupiah', 'ID', 'Asia', ['dana_id', 'ovo_id', 'gopay_id']),
    ('MYR', 'Malaysian ringgit', 'MY', 'Asia', ['duitnow_my', 'tng_my']),
    ('SGD', 'Singapore dollar', 'SG', 'Asia', ['paynow_sg']),
    ('THB', 'Thai baht', 'TH', 'Asia', ['promptpay_th', 'truemoney_th']),
    ('VND', 'Vietnamese dong', 'VN', 'Asia', ['momo_vn', 'zalopay_vn']),
    ('KHR', 'Cambodian riel', 'KH', 'Asia', ['aba_kh', 'wing_kh']),
    ('LAK', 'Lao kip', 'LA', 'Asia', ['bcel_la']),
    ('MMK', 'Myanmar kyat', 'MM', 'Asia', ['kbzpay_mm', 'wavepay_mm']),
    ('MNT', 'Mongolian tögrög', 'MN', 'Asia', ['qpay_mn']),
    ('KZT', 'Kazakhstani tenge', 'KZ', 'Asia', ['kaspi_kz']),
    ('UZS', 'Uzbekistani som', 'UZ', 'Asia', ['click_uz', 'payme_uz']),
    ('KGS', 'Kyrgyzstani som', 'KG', 'Asia', ['mbank_kg']),
    ('TJS', 'Tajikistani somoni', 'TJ', 'Asia', ['alif_tj']),
    ('AZN', 'Azerbaijani manat', 'AZ', 'Asia', ['m10_az']),
    ('GEL', 'Georgian lari', 'GE', 'Asia', ['tbc_ge']),
    ('AMD', 'Armenian dram', 'AM', 'Asia', ['idram_am']),
    ('BND', 'Brunei dollar', 'BN', 'Asia', []),
    ('MVR', 'Maldivian rufiyaa', 'MV', 'Asia', []),
    ('BTN', 'Bhutanese ngultrum', 'BT', 'Asia', []),
    # Middle East
    ('AED', 'UAE dirham', 'AE', 'Middle East', ['payit_ae']),
    ('SAR', 'Saudi riyal', 'SA', 'Middle East', ['stcpay_sa', 'urpay_sa']),
    ('QAR', 'Qatari riyal', 'QA', 'Middle East', []),
    ('KWD', 'Kuwaiti dinar', 'KW', 'Middle East', []),
    ('BHD', 'Bahraini dinar', 'BH', 'Middle East', ['benefitpay_bh']),
    ('OMR', 'Omani rial', 'OM', 'Middle East', []),
    ('JOD', 'Jordanian dinar', 'JO', 'Middle East', ['cliq_jo']),
    ('ILS', 'Israeli shekel', 'IL', 'Middle East', ['bit_il', 'paybox_il']),
    ('TRY', 'Turkish lira', 'TR', 'Middle East', ['papara_tr', 'havale_tr']),
    ('LBP', 'Lebanese pound', 'LB', 'Middle East', ['whish_lb', 'omt_lb']),
    ('IQD', 'Iraqi dinar', 'IQ', 'Middle East', ['zaincash_iq', 'fastpay_iq']),
    ('YER', 'Yemeni rial', 'YE', 'Middle East', ['jaib_ye']),
    # Europe
    ('EUR', 'Euro', 'EU', 'Europe', ['sepa_eu', 'sepa_instant_eu', 'bizum_es', 'satispay_it', 'lydia_fr', 'ideal_nl', 'mbway_pt', 'paylib_fr', 'payconiq_be']),
    ('GBP', 'British pound', 'GB', 'Europe', ['faster_payments_gb', 'monzo_gb']),
    ('CHF', 'Swiss franc', 'CH', 'Europe', ['twint_ch']),
    ('SEK', 'Swedish krona', 'SE', 'Europe', ['swish_se']),
    ('NOK', 'Norwegian krone', 'NO', 'Europe', ['vipps_no']),
    ('DKK', 'Danish krone', 'DK', 'Europe', ['mobilepay_dk']),
    ('ISK', 'Icelandic króna', 'IS', 'Europe', []),
    ('PLN', 'Polish złoty', 'PL', 'Europe', ['blik_pl']),
    ('CZK', 'Czech koruna', 'CZ', 'Europe', ['qr_cz']),
    ('HUF', 'Hungarian forint', 'HU', 'Europe', []),
    ('RON', 'Romanian leu', 'RO', 'Europe', ['revolut_ro']),
    ('BGN', 'Bulgarian lev', 'BG', 'Europe', []),
    ('RSD', 'Serbian dinar', 'RS', 'Europe', []),
    ('UAH', 'Ukrainian hryvnia', 'UA', 'Europe', ['privatbank_ua', 'monobank_ua']),
    ('MDL', 'Moldovan leu', 'MD', 'Europe', ['mia_md']),
    ('ALL', 'Albanian lek', 'AL', 'Europe', []),
    ('MKD', 'Macedonian denar', 'MK', 'Europe', []),
    ('BAM', 'Bosnian convertible mark', 'BA', 'Europe', []),
    # Americas
    ('USD', 'US dollar', 'US', 'Americas', ['bank_us', 'zelle_us', 'cashapp_us', 'venmo_us', 'chime_us', 'apple_cash_us']),
    ('CAD', 'Canadian dollar', 'CA', 'Americas', ['interac_ca']),
    ('MXN', 'Mexican peso', 'MX', 'Americas', ['spei_mx', 'oxxo_mx', 'mercadopago']),
    ('BRL', 'Brazilian real', 'BR', 'Americas', ['pix_br', 'mercadopago']),
    ('ARS', 'Argentine peso', 'AR', 'Americas', ['mercadopago', 'cvu_ar', 'uala_ar', 'brubank_ar']),
    ('CLP', 'Chilean peso', 'CL', 'Americas', ['mach_cl', 'transfer_cl', 'mercadopago']),
    ('COP', 'Colombian peso', 'CO', 'Americas', ['nequi_co', 'daviplata_co', 'bancolombia_co']),
    ('PEN', 'Peruvian sol', 'PE', 'Americas', ['yape_pe', 'plin_pe']),
    ('VES', 'Venezuelan bolívar', 'VE', 'Americas', ['pagomovil_ve', 'zinli_ve']),
    ('BOB', 'Bolivian boliviano', 'BO', 'Americas', ['tigomoney_bo']),
    ('PYG', 'Paraguayan guaraní', 'PY', 'Americas', ['tigomoney_py']),
    ('UYU', 'Uruguayan peso', 'UY', 'Americas', ['prex_uy', 'mercadopago']),
    ('CRC', 'Costa Rican colón', 'CR', 'Americas', ['sinpe_cr']),
    ('GTQ', 'Guatemalan quetzal', 'GT', 'Americas', ['tigomoney_gt']),
    ('HNL', 'Honduran lempira', 'HN', 'Americas', ['tigomoney_hn']),
    ('NIO', 'Nicaraguan córdoba', 'NI', 'Americas', []),
    ('PAB', 'Panamanian balboa', 'PA', 'Americas', ['yappy_pa']),
    ('DOP', 'Dominican peso', 'DO', 'Americas', ['tpago_do']),
    ('HTG', 'Haitian gourde', 'HT', 'Americas', ['moncash_ht', 'natcash_ht']),
    ('JMD', 'Jamaican dollar', 'JM', 'Americas', ['lynk_jm']),
    ('TTD', 'Trinidad & Tobago dollar', 'TT', 'Americas', ['linx_tt']),
    ('BSD', 'Bahamian dollar', 'BS', 'Americas', []),
    ('BBD', 'Barbadian dollar', 'BB', 'Americas', []),
    ('BZD', 'Belize dollar', 'BZ', 'Americas', []),
    ('XCD', 'East Caribbean dollar', 'AG', 'Americas', []),
    ('GYD', 'Guyanese dollar', 'GY', 'Americas', []),
    ('SRD', 'Surinamese dollar', 'SR', 'Americas', []),
    # Oceania
    ('AUD', 'Australian dollar', 'AU', 'Oceania', ['payid_au']),
    ('NZD', 'New Zealand dollar', 'NZ', 'Oceania', ['bank_nz']),
    ('FJD', 'Fijian dollar', 'FJ', 'Oceania', ['mpaisa_fj', 'mycash_fj']),
    ('PGK', 'Papua New Guinean kina', 'PG', 'Oceania', ['cellmoni_pg']),
]

REGIONS = ['Africa', 'Asia', 'Middle East', 'Europe', 'Americas', 'Oceania']
CURRENCIES = {code: (name, _flag(cc), region, local + [m for m in UNIVERSAL if m not in local])
              for code, name, cc, region, local in _C}


def grouped():
    """[(region, [(code, name, flag), …]), …] for <optgroup> selects."""
    return [(r, [(c, v[0], v[1]) for c, v in CURRENCIES.items() if v[2] == r]) for r in REGIONS]
