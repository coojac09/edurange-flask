import json
import os
import random
import shutil
import string
from datetime import datetime
from os import environ

from celery import Celery
from celery.utils.log import get_task_logger
from flask import current_app, flash, render_template, session
from flask_mail import Mail, Message

from edurange_refactored.scenario_utils import (
    begin_tf_and_write_providers,
    gather_files,
    known_types,
    write_container,
    write_output_block, write_network,
)
from edurange_refactored.settings import CELERY_BROKER_URL, CELERY_RESULT_BACKEND

logger = get_task_logger(__name__)

path_to_directory = os.path.dirname(os.path.abspath(__file__))


def get_path(file_name):
    mail_path = os.path.normpath(
        os.path.join(path_to_directory, "templates/utils", file_name)
    )
    return mail_path


celery = Celery(__name__, broker=CELERY_BROKER_URL, backend=CELERY_RESULT_BACKEND)


class ContextTask(celery.Task):
    abstract = True

    def __call__(self, *args, **kwargs):
        from edurange_refactored.app import create_app

        with create_app().app_context():
            return super(ContextTask, self).__call__(*args, **kwargs)


celery.Task = ContextTask


@celery.task
def send_async_email(email_data):
    app = current_app
    app.config.update(
        MAIL_SERVER="smtp.googlemail.com",
        MAIL_PORT=465,
        MAIL_USE_TLS=False,
        MAIL_USE_SSL=True,
    )
    mail = Mail(app)
    msg = Message(
        email_data["subject"],
        sender=environ.get("MAIL_DEFAULT_SENDER"),
        recipients=[email_data["to"]],
    )
    mail.send(msg)
    msg.body = email_data["body"]


@celery.task
def test_send_async_email(email_data):
    app = current_app
    mail = Mail(app)
    msg = Message(
        email_data["subject"],
        sender=environ.get("MAIL_DEFAULT_SENDER"),
        recipients=[email_data["email"]],
    )

    msg.body = render_template(
        "utils/reset_password_email.txt",
        token=email_data["token"],
        email=email_data["email"],
        _external=True,
    )
    msg.html = render_template(
        "utils/reset_password_email.html",
        token=email_data["token"],
        email=email_data["email"],
        _external=True,
    )
    mail.send(msg)


@celery.task(bind=True)
def CreateScenarioTask(self, name, s_type, owner, group, g_id, s_id):
    from edurange_refactored.user.models import ScenarioGroups

    app = current_app
    s_type = s_type.lower()
    s_id = s_id["id"]
    g_id = g_id["id"]

    c_names, g_files, s_files, u_files, packages, ip_addrs = gather_files(s_type, logger)

    logger.info(
        "Executing task id {0.id}, args: {0.args!r} kwargs: {0.kwargs!r}".format(
            self.request
        )
    )
    students = {}
    usernames = []
    passwords = []

    for i in range(len(group)):
        username = "".join(e for e in group[i]["username"] if e.isalnum())
        password = "".join(
            random.choice(string.ascii_letters + string.digits) for _ in range(8)
        )

        usernames.append(username)
        passwords.append(password)

        logger.info("User: {}".format(group[i]["username"]))
        students[username] = []
        students[username].append({"username": username, "password": password})

    logger.info("All names: {}".format(students))

    with app.test_request_context():

        name = "".join(e for e in name if e.isalnum())
        own_id = owner

        os.mkdir("./data/tmp/" + name)
        os.chdir("./data/tmp/" + name)

        with open("students.json", "w") as outfile:
            json.dump(students, outfile)

        begin_tf_and_write_providers(name)

        if s_type == "ssh_inception" or s_type == "total_recon":
            write_network(name)

        for i, c in enumerate(c_names):
            write_container(
                name + "_" + c,
                s_type,
                usernames,
                passwords,
                g_files[i],
                s_files[i],
                u_files[i],
                packages[i],
                #ip_addrs[i]
            )

        write_output_block(name, c_names)

        os.system("terraform init")
        os.chdir("../../..")

        ScenarioGroups.create(group_id=g_id, scenario_id=s_id)


@celery.task(bind=True)
def start(self, sid):
    from edurange_refactored.user.models import Scenarios

    app = current_app
    logger.info(
        "Executing task id {0.id}, args: {0.args!r} kwargs: {0.kwargs!r}".format(
            self.request
        )
    )
    with app.test_request_context():
        scenario = Scenarios.query.filter_by(id=sid).first()
        logger.info("Found Scenario: {}".format(scenario))
        name = str(scenario.name)
        if int(scenario.status) != 0:
            logger.info("Invalid Status")
            raise Exception(f"Scenario must be stopped before starting")
        elif os.path.isdir(os.path.join("./data/tmp/", name)):
            scenario.update(status=3)
            logger.info("Folder Found")
            os.chdir("./data/tmp/" + name)
            os.system("terraform apply --auto-approve")
            os.chdir("../../..")
            scenario.update(status=1)
        else:
            logger.info("Something went wrong")
            flash("Something went wrong", "warning")


@celery.task(bind=True)
def stop(self, sid):
    from edurange_refactored.user.models import Scenarios

    app = current_app
    logger.info(
        "Executing task id {0.id}, args: {0.args!r} kwargs: {0.kwargs!r}".format(
            self.request
        )
    )
    with app.test_request_context():
        scenario = Scenarios.query.filter_by(id=sid).first()
        logger.info("Found Scenario: {}".format(scenario))
        name = str(scenario.name)
        if int(scenario.status) != 1:
            logger.info("Invalid Status")
            flash("Scenario is not ready to start", "warning")
        elif os.path.isdir(os.path.join("./data/tmp/", name)):
            logger.info("Folder Found")
            scenario.update(status=4)
            os.chdir("./data/tmp/" + name)
            os.system("terraform destroy --auto-approve")
            os.chdir("../../..")
            scenario.update(status=0)
        else:
            logger.info("Something went wrong")
            flash("Something went wrong", "warning")


@celery.task(bind=True)
def destroy(self, sid):
    from edurange_refactored.user.models import Scenarios, ScenarioGroups

    app = current_app
    logger.info(
        "Executing task id {0.id}, args: {0.args!r} kwargs: {0.kwargs!r}".format(
            self.request
        )
    )
    with app.test_request_context():
        scenario = Scenarios.query.filter_by(id=sid).first()
        if scenario is not None:
            logger.info("Found Scenario: {}".format(scenario))
            name = str(scenario.name)
            s_id = str(scenario.id)
            s_group = ScenarioGroups.query.filter_by(scenario_id=s_id).first()
            if int(scenario.status) != 0:
                logger.info("Invalid Status")
                raise Exception(f"Scenario in an Invalid state for Destruction")
            elif os.path.isdir(os.path.join("./data/tmp/", name)):
                logger.info("Folder Found, current directory: {}".format(os.getcwd()))
                os.chdir("./data/tmp/")
                shutil.rmtree(name)
                os.chdir("../..")
                s_group.delete()
                scenario.delete()
            else:
                logger.info("Something went wrong")
                flash("Something went wrong", "warning")
        else:
            raise Exception(f"Could not find scenario")

#global scenarios_dict to keep track of status
scenarios_dict = {}
@celery.task(bind=True)
#def scenarioTimeoutWarningEmail(self):
def scenarioTimeoutWarningEmail(self, arg):
    from edurange_refactored.user.models import Scenarios
    from edurange_refactored.user.models import User
    scenarios = Scenarios.query.all()
    users = User.query.all()
    global scenarios_dict
    for scenario in scenarios:
        for user in users:
            #Add new scenarios to dict
            if scenario.id in scenarios_dict.keys() == False:
                scenarios_dict = {scenario.id : scenario.status}
            #If current status is running and dict scenario running status = true then send warning email
            elif scenario.id in scenarios_dict.keys() and scenarios_dict[scenario.id] == 1 and scenario.status == 1:
                if user.id == scenario.owner_id:
                    email = user.email
                    email_data = {"subject": "WARNING: Scenario Running Too Long", "email": email}
                    app = current_app
                    mail = Mail(app)
                    msg = Message(email_data['subject'],
                                sender=environ.get('MAIL_DEFAULT_SENDER'),
                                recipients=[email_data['email']])
                    msg.body = render_template('utils/scenario_timeout_warning_email.txt', email=email_data['email'], _external=True)
                    msg.html = render_template('utils/scenario_timeout_warning_email.html', email=email_data['email'], _external=True)
                    mail.send(msg)
            #update dict with current status
            scenarios_dict[scenario.id] = scenario.status
    #    print(arg)
    #email_data = {'subject': 'WARNING: Scenario Running Too Long', 'to': 'selenawalshsmith@gmail.com', 'body':'WARNING: Scenario Running Too Long'}
    #send_async_email(email_data)
@celery.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    #21600 is 6 hrs in seconds
    sender.add_periodic_task(21600.0, scenarioTimeoutWarningEmail.s('******Hello World from Selena*********'))
