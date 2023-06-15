import filecmp
import io
import os.path
import pathlib
import shutil
import zipfile

from aggregators import handle_aggregators_post_request
from flask import Flask, flash, render_template, request, redirect, send_file, url_for
from os import urandom, path
from typing import List
from utils import RESTART, ENV_FILE, print_err
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = urandom(16).hex()


@app.route("/propagateTZ")
def get_tz():
    browser_timezone = request.args.get("tz")
    env_values = ENV_FILE.envs
    env_values["FEEDER_TZ"] = browser_timezone
    return render_template(
        "index.html", env_values=env_values, metadata=ENV_FILE.metadata
    )


@app.route("/restarting", methods=(["GET"]))
def restarting():
    return render_template(
        "restarting.html", env_values=ENV_FILE.envs, metadata=ENV_FILE.metadata
    )


@app.route("/restart", methods=(["GET", "POST"]))
def restart():
    if request.method == "POST":
        RESTART.restart_systemd()
        return "restarting" if restart else "already restarting"
    if request.method == "GET":
        return RESTART.state


@app.route("/backup")
def backup():
    adsb_path = pathlib.Path("/opt/adsb")
    data = io.BytesIO()
    with zipfile.ZipFile(data, mode="w") as backup_zip:
        backup_zip.write(adsb_path / ".env", arcname=".env")
        for f in adsb_path.glob("*.yml"):
            backup_zip.write(f, arcname=os.path.basename(f))
        uf_path = pathlib.Path(adsb_path / "ultrafeeder")
        if uf_path.is_dir():
            backup_zip.mkdir("ultrafeeder")
            for f in uf_path.rglob("*"):
                    backup_zip.write(f)
    data.seek(0)
    return send_file(data, mimetype="application/zip", as_attachment=True, download_name="adsb-feeder-config.zip")


@app.route("/restore", methods=['GET', 'POST'])
def restore():
    if request.method == 'POST':
        # check if the post request has the file part
        if 'file' not in request.files:
            flash('No file submitted')
            return redirect(request.url)
        file = request.files['file']
        # If the user does not select a file, the browser submits an
        # empty file without a filename.
        if file.filename == '':
            flash('No file selected')
            return redirect(request.url)
        if file.filename.endswith(".zip"):
            filename = secure_filename(file.filename)
            restore_path = pathlib.Path("/opt/adsb/restore")
            restore_path.mkdir(mode=0o644, exist_ok=True)
            file.save(restore_path / filename)
            print_err(f"saved restore file to {restore_path / filename}")
            return redirect(url_for("executerestore", zipfile=filename))
        else:
            flash("Please only submit ADSB Feeder Image backup files")
            return redirect(request.url)
    else:
        return render_template("/restore.html", metadata=ENV_FILE.metadata)


@app.route("/executerestore", methods=["GET", "POST"])
def executerestore():
    if request.method == "GET":
        # the user has uploaded a zip file and we need to take a look.
        # be very careful with the content of this zip file...
        filename = request.args['zipfile']
        adsb_path = pathlib.Path("/opt/adsb")
        restore_path = pathlib.Path("/opt/adsb/restore")
        restored_files: List[str] = []
        with zipfile.ZipFile(restore_path / filename, "r") as restore_zip:
            for name in restore_zip.namelist():
                print_err(f"found file {name} in archive")
                # only accept the .env file and simple .yml filenames
                if name != ".env" and (not name.endswith(".yml") or name != secure_filename(name)):
                    continue
                restore_zip.extract(name, restore_path)
                restored_files.append(name)
        # now check which ones are different from the installed versions
        changed: List[str] = []
        unchanged: List[str] = []
        for name in restored_files:
            if os.path.isfile(adsb_path / name):
                if filecmp.cmp(adsb_path / name, restore_path / name):
                    print_err(f"{name} is different from current version")
                    unchanged.append(name)
                else:
                    print_err(f"{name} is unchanged")
                    changed.append(name)
        metadata = ENV_FILE.metadata
        metadata["changed"] = changed
        metadata["unchanged"] = unchanged
        return render_template("/restoreexecute.html", metadata=metadata)
    else:
        # they have selected the files to restore
        restore_path = pathlib.Path("/opt/adsb/restore")
        adsb_path = pathlib.Path("/opt/adsb")
        for name in request.form.keys():
            print_err(f"restoring {name}")
            shutil.move(adsb_path / name, restore_path / (name + ".dist"))
            shutil.move(restore_path / name, adsb_path / name)
        return redirect("/advanced")  # that's a good place from where the user can continue


@app.route("/advanced", methods=("GET", "POST"))
def advanced():
    if request.method == "POST":
        return handle_advanced_post_request()
    env_values = ENV_FILE.envs
    if RESTART.lock.locked():
        return redirect("/restarting")
    return render_template(
        "advanced.html", env_values=env_values, metadata=ENV_FILE.metadata
    )


def handle_advanced_post_request():
    print("request_form", request.form)
    if request.form.get("submit") == "go":
        ENV_FILE.update(
            {
                "FEEDER_TAR1090_USEROUTEAPI": "1" if request.form.get("route") else "0",
                "MLAT_PRIVACY": "--privacy" if request.form.get("privacy") else "",
            }
        )
    net = ENV_FILE.generate_ultrafeeder_config(request.form)
    ENV_FILE.update({"FEEDER_ULTRAFEEDER_CONFIG": net})
    if request.form.get("tar1090") == "go":
        host, port = request.server
        tar1090 = request.url_root.replace(str(port), "8080")
        return redirect(tar1090)

    return redirect("/restarting")


@app.route("/expert", methods=("GET", "POST"))
def expert():
    if request.method == "POST":
        return handle_expert_post_request()
    env_values = ENV_FILE.envs
    if RESTART.lock.locked():
        return redirect("/restarting")
    filecontent = {'have_backup': False}
    if path.exists("/opt/adsb/env-working") and path.exists("/opt/adsb/docker-compose.yml-working"):
        filecontent['have_backup'] = True
    with open("/opt/adsb/.env", "r") as env:
        filecontent['env'] = env.read()
    with open("/opt/adsb/docker-compose.yml") as dc:
        filecontent['dc'] = dc.read()
    return render_template(
        "expert.html", env_values=env_values, metadata=ENV_FILE.metadata, filecontent=filecontent
    )


def handle_expert_post_request():
    if request.form.get("you-asked-for-it") == "you-got-it":
        # well - let's at least try to save the old stuff
        if not path.exists("/opt/adsb/env-working"):
            try:
                shutil.copyfile("/opt/adsb/.env", "/opt/adsb/env-working")
            except shutil.Error as err:
                print(f"copying .env didn't work: {err.args[0]}: {err.args[1]}")
        if not path.exists("/opt/adsb/dc-working"):
            try:
                shutil.copyfile("/opt/adsb/docker-compose.yml", "/opt/adsb/docker-compose.yml-working")
            except shutil.Error as err:
                print(f"copying docker-compose.yml didn't work: {err.args[0]}: {err.args[1]}")
        with open("/opt/adsb/.env", "w") as env:
            env.write(request.form["env"])
        with open("/opt/adsb/docker-compose.yml", "w") as dc:
            dc.write(request.form["dc"])

        RESTART.restart_systemd()
        return redirect("restarting")

    if request.form.get("you-got-it") == "give-it-back":
        # do we have saved old files?
        if path.exists("/opt/adsb/env-working"):
            try:
                shutil.copyfile("/opt/adsb/env-working", "/opt/adsb/.env")
            except shutil.Error as err:
                print(f"copying .env didn't work: {err.args[0]}: {err.args[1]}")
        if path.exists("/opt/adsb/docker-compose.yml-working"):
            try:
                shutil.copyfile("/opt/adsb/docker-compose.yml-working", "/opt/adsb/docker-compose.yml")
            except shutil.Error as err:
                print(f"copying docker-compose.yml didn't work: {err.args[0]}: {err.args[1]}")

        RESTART.restart_systemd()
        return redirect("restarting")

    print("request_form", request.form)
    return redirect("/advanced")


@app.route("/aggregators", methods=("GET", "POST"))
def aggregators():
    if RESTART.lock.locked():
        return redirect("/restarting")
    if request.method == "POST":
        return handle_aggregators_post_request()
    env_values = ENV_FILE.envs
    return render_template(
        "aggregators.html", env_values=env_values, metadata=ENV_FILE.metadata
    )


@app.route("/", methods=("GET", "POST"))
def setup():
    if request.args.get("success"):
        return redirect("/advanced")
    if RESTART.lock.locked():
        return redirect("/restarting")

    if request.method == "POST":
        lat, lng, alt, form_timezone, mlat_name, agg = (
            request.form[key]
            for key in ["lat", "lng", "alt", "form_timezone", "mlat_name", "aggregators", ]
        )
        print_err(f"got lat: {lat}, lng: {lng}, alt: {alt}, TZ: {form_timezone}, mlat-name: {mlat_name}, agg: {agg}")
        if all([lat, lng, alt, form_timezone]):
            net = ENV_FILE.generate_ultrafeeder_config(request.form)
            ENV_FILE.update(
                {
                    "FEEDER_LAT": lat,
                    "FEEDER_LONG": lng,
                    "FEEDER_ALT_M": alt,
                    "FEEDER_TZ": form_timezone,
                    "MLAT_SITE_NAME": mlat_name,
                    "FEEDER_AGG": agg,
                    "FEEDER_ULTRAFEEDER_CONFIG": net,
                }
            )
            return redirect("/restarting")

    return render_template(
        "index.html", env_values=ENV_FILE.envs, metadata=ENV_FILE.metadata
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
