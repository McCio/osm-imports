
env:
	python3 -m venv venv
	venv/bin/python3 -m pip install --upgrade pip
	venv/bin/python3 -m pip install --requirement requirements.txt
	patch venv/lib/python3.1?/site-packages/conflate/geocoder.py geocoder.patch

update:
	curl --etag-save data/iccu.etag --etag-compare data/iccu.etag --silent -o data/iccu.zip https://opendata.anagrafe.iccu.sbn.it/opendata.zip
	bsdtar -x -f data/iccu.zip -C data/

clean:
	venv/bin/python3 clean.py

conflate:
	venv/bin/conflate --source data/clean.csv --output data/changes.osc --changes data/changes.geojson --regions italy --osm data/osm.osm profile.py

