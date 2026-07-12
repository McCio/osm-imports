source = 'https://anagrafe.iccu.sbn.it/'  # https://anagrafe.iccu.sbn.it/open-data/
add_source = False
dataset_id = 'isil'
query = [('amenity', 'library')]
#bbox = [ 35.5, 6.7, 47.1, 18.5 ]  # True  # how to set italy?
bounded_update = True
regions = 'it'
max_distance = 200  # meters
duplicate_distance = 1
master_tags = ('ref:isil', 'ref:sbn', 'ref:acnp', 'ref:cei', 'ref:cmbs', 'ref:rism', 'official_name', 'operator',  'addr:housenumber', 'contact:phone', 'contact:website')

def dataset(f):
  import polars as pl
  from conflate import SourcePoint
  # data-aggiornamento,codice-isil,acnp,cei,cmbs,rism,sbn,denominazione,denominazioni-precedenti,denominazioni-alternative,tipologia-amministrativa,tipologia-funzionale,ente,fondo-speciale,materiale,indirizzo,frazione,cap,comune,provincia,regione,latitudine,longitudine,access,wheelchair,valore_contact_email,valore_contact_pec,valore_contact_fax,valore_contact_website,valore_contact_phone,valore_contact_facebook,valore_contact_instagram,valore_contact_twitter,note_contact_email,note_contact_pec,note_contact_fax,note_contact_website,note_contact_phone,note_contact_facebook,note_contact_instagram,note_contact_twitter,address_more_info,address_housenumber,address_street
  col_mapper = {
    'codice-isil': 'ref:isil',
    'acnp': 'ref:acnp',
    'cei': 'ref:cei',
    'cmbs': 'ref:cmbs',
    'rism': 'ref:rism',
    'sbn': 'ref:sbn',
    'denominazione': 'official_name',
    'denominazioni-precedenti': 'old_name',
    'denominazioni-alternative': 'alt_name',
    'address_street': 'addr:street',
    'address_housenumber': 'addr:housenumber',
    'valore_contact_phone': 'contact:phone',
    'valore_contact_fax': 'contact:fax',
    'valore_contact_email': 'contact:email',
    'valore_contact_website': 'contact:website',
    'valore_contact_instagram': 'contact:instagram',
    'valore_contact_facebook': 'contact:facebook',
    'valore_contact_twitter': 'contact:twitter',
    'access': 'access',
    'wheelchair': 'wheelchair',
    'cap': 'addr:postcode',
    'ente': 'operator',
  }
  csv = pl.read_csv(f).filter(~(pl.col('latitudine').is_null() | pl.col('longitudine').is_null())).rename(col_mapper)
  rows_to_use = [*col_mapper.values(), 'latitudine', 'longitudine']
  for row in csv.select(*rows_to_use).with_row_count(offset=1).to_dicts():
    if old_name := row['old_name']:
      row['old_name'] = old_name.split(';')[0]
    if alt_name := row['alt_name']:
      row['alt_name'] = alt_name.split(';')[0]
    el = {
      'pid': row['ref:isil'],
      'lat': row['latitudine'],
      'lon': row['longitudine'],
      'tags': row,
    }
    yield SourcePoint(**el)

transform = {
  'row_nr': '-',
  'latitudine': '-',
  'longitudine': '-',
  'phone': '>contact:phone',
  'amenity': 'library',
  'name': '.official_name',
}

