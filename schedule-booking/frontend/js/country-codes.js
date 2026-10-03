// Country calling codes for the booking form's mobile field.
// Each entry: [ISO 3166-1 alpha-2 abbreviation, country name, calling code without "+"].
// "priority" (Saudi Arabia) is shown first, then a divider, then every other country A-Z.
// Shared calling codes (e.g. +1, +7) simply appear once per country.

window.ZA_COUNTRIES = (function () {
    var priority = [
        ['SA', 'Saudi Arabia', '966']
    ];

    var others = [
        ['AE', 'United Arab Emirates', '971'], ['BH', 'Bahrain', '973'], ['EG', 'Egypt', '20'], ['GB', 'United Kingdom', '44'],
        ['KW', 'Kuwait', '965'], ['OM', 'Oman', '968'], ['QA', 'Qatar', '974'],
        ['AF', 'Afghanistan', '93'], ['AL', 'Albania', '355'], ['DZ', 'Algeria', '213'], ['AD', 'Andorra', '376'],
        ['AO', 'Angola', '244'], ['AR', 'Argentina', '54'], ['AM', 'Armenia', '374'], ['AU', 'Australia', '61'],
        ['AT', 'Austria', '43'], ['AZ', 'Azerbaijan', '994'], ['BD', 'Bangladesh', '880'], ['BY', 'Belarus', '375'],
        ['BE', 'Belgium', '32'], ['BJ', 'Benin', '229'], ['BT', 'Bhutan', '975'], ['BO', 'Bolivia', '591'],
        ['BA', 'Bosnia and Herzegovina', '387'], ['BW', 'Botswana', '267'], ['BR', 'Brazil', '55'], ['BN', 'Brunei', '673'],
        ['BG', 'Bulgaria', '359'], ['BF', 'Burkina Faso', '226'], ['KH', 'Cambodia', '855'], ['CM', 'Cameroon', '237'],
        ['CA', 'Canada', '1'], ['CL', 'Chile', '56'], ['CN', 'China', '86'], ['CO', 'Colombia', '57'],
        ['CG', 'Congo', '242'], ['CD', 'Congo (DR)', '243'], ['CR', 'Costa Rica', '506'], ['CI', 'Cote d\'Ivoire', '225'],
        ['HR', 'Croatia', '385'], ['CU', 'Cuba', '53'], ['CY', 'Cyprus', '357'], ['CZ', 'Czechia', '420'],
        ['DK', 'Denmark', '45'], ['DJ', 'Djibouti', '253'], ['DO', 'Dominican Republic', '1'], ['EC', 'Ecuador', '593'],
        ['SV', 'El Salvador', '503'], ['EE', 'Estonia', '372'], ['ET', 'Ethiopia', '251'], ['FI', 'Finland', '358'],
        ['FR', 'France', '33'], ['GE', 'Georgia', '995'], ['DE', 'Germany', '49'], ['GH', 'Ghana', '233'],
        ['GR', 'Greece', '30'], ['GT', 'Guatemala', '502'], ['HK', 'Hong Kong', '852'], ['HU', 'Hungary', '36'],
        ['IS', 'Iceland', '354'], ['IN', 'India', '91'], ['ID', 'Indonesia', '62'], ['IR', 'Iran', '98'],
        ['IQ', 'Iraq', '964'], ['IE', 'Ireland', '353'], ['IL', 'Israel', '972'], ['IT', 'Italy', '39'],
        ['JM', 'Jamaica', '1'], ['JP', 'Japan', '81'], ['JO', 'Jordan', '962'], ['KZ', 'Kazakhstan', '7'],
        ['KE', 'Kenya', '254'], ['XK', 'Kosovo', '383'], ['KG', 'Kyrgyzstan', '996'], ['LA', 'Laos', '856'],
        ['LV', 'Latvia', '371'], ['LB', 'Lebanon', '961'], ['LY', 'Libya', '218'], ['LT', 'Lithuania', '370'],
        ['LU', 'Luxembourg', '352'], ['MO', 'Macao', '853'], ['MG', 'Madagascar', '261'], ['MY', 'Malaysia', '60'],
        ['MV', 'Maldives', '960'], ['ML', 'Mali', '223'], ['MT', 'Malta', '356'], ['MR', 'Mauritania', '222'],
        ['MU', 'Mauritius', '230'], ['MX', 'Mexico', '52'], ['MD', 'Moldova', '373'], ['MN', 'Mongolia', '976'],
        ['ME', 'Montenegro', '382'], ['MA', 'Morocco', '212'], ['MZ', 'Mozambique', '258'], ['MM', 'Myanmar', '95'],
        ['NP', 'Nepal', '977'], ['NL', 'Netherlands', '31'], ['NZ', 'New Zealand', '64'], ['NG', 'Nigeria', '234'],
        ['MK', 'North Macedonia', '389'], ['NO', 'Norway', '47'], ['PK', 'Pakistan', '92'], ['PS', 'Palestine', '970'],
        ['PA', 'Panama', '507'], ['PE', 'Peru', '51'], ['PH', 'Philippines', '63'], ['PL', 'Poland', '48'],
        ['PT', 'Portugal', '351'], ['PR', 'Puerto Rico', '1'], ['RO', 'Romania', '40'], ['RU', 'Russia', '7'],
        ['RW', 'Rwanda', '250'], ['SN', 'Senegal', '221'], ['RS', 'Serbia', '381'], ['SG', 'Singapore', '65'],
        ['SK', 'Slovakia', '421'], ['SI', 'Slovenia', '386'], ['SO', 'Somalia', '252'], ['ZA', 'South Africa', '27'],
        ['KR', 'South Korea', '82'], ['SS', 'South Sudan', '211'], ['ES', 'Spain', '34'], ['LK', 'Sri Lanka', '94'],
        ['SD', 'Sudan', '249'], ['SE', 'Sweden', '46'], ['CH', 'Switzerland', '41'], ['SY', 'Syria', '963'],
        ['TW', 'Taiwan', '886'], ['TJ', 'Tajikistan', '992'], ['TZ', 'Tanzania', '255'], ['TH', 'Thailand', '66'],
        ['TN', 'Tunisia', '216'], ['TR', 'Turkey', '90'], ['TM', 'Turkmenistan', '993'], ['UG', 'Uganda', '256'],
        ['UA', 'Ukraine', '380'], ['US', 'United States', '1'], ['UY', 'Uruguay', '598'], ['UZ', 'Uzbekistan', '998'],
        ['VE', 'Venezuela', '58'], ['VN', 'Vietnam', '84'], ['YE', 'Yemen', '967'], ['ZM', 'Zambia', '260'],
        ['ZW', 'Zimbabwe', '263']
    ];

    others.sort(function (a, b) { return a[1] < b[1] ? -1 : (a[1] > b[1] ? 1 : 0); });
    return { priority: priority, others: others };
})();
