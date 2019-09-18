import os
import sys
import json
import re
import time
import datetime
import traceback
from collections import defaultdict, Counter
import warnings

from flask import Flask
import click
from flask import render_template, request
from addict import Dict
import arrow
from arrow.factory import ArrowParseWarning

import pgraph
import repository_summarizer
import vulnerability_summarizer
import stats
import dependabot_api
import github_rest_client
import config
import storage


warnings.simplefilter("ignore", ArrowParseWarning)
app = Flask(__name__, static_url_path="/assets")
settings = config.load()

if settings.aws_region:
    storage.set_region(config.get_value("aws_region"))

if settings.storage:
    storage_options = config.get_value("storage")
    storage.set_options(storage_options)


@app.template_filter("iso_date")
def filter_iso_date(iso_date):
    """Convert a string to all caps."""
    parsed = arrow.get(iso_date, "YYYY-MM-DD")
    return parsed.format("DD/MM/YYYY")


@app.template_filter("abbreviate")
def filter_abbreviate(word):
    lowercase_word = word.lower()
    abbrevs = {
        "critical": "crit",
        "moderate": "mod",
        "medium": "med",
        "dependabot": "dbot",
        "advisory": "adv",
    }
    return abbrevs[lowercase_word] if (lowercase_word in abbrevs) else word


def get_history():
    history_file = "all/data/history.json"

    default = Dict({"current": None, "alltime": {}})
    history = storage.read_json(history_file, default=default)

    print(str(history), sys.stderr)

    return history


def update_history(history):
    return storage.save_json("all/data/history.json", history)


def get_current_audit():
    try:
        history = get_history()
        current = history.current
    except FileNotFoundError:
        current = datetime.date.today().isoformat()
    return current


@app.cli.command("audit")
def cronable_vulnerability_audit():
    today = datetime.date.today().isoformat()

    # set status to inprogress in history
    history = get_history()
    history.alltime[today] = "in progress"
    update_history(history)

    # retrieve data from apis
    org = config.get_value("github_org")
    # todo - set maintenance mode
    repository_status = get_github_repositories_and_classify_by_status(org, today)
    refs_status = get_github_activity_refs_audit(org, today)
    prs_status = get_github_activity_prs_audit(org, today)
    dbot_status = get_dependabot_status(org, today)

    if history.current:
        update_github_advisories_status()
    else:
        get_github_resolve_alert_status()

    # analyse raw data
    ownership_status = analyse_repo_ownership(today)
    pr_status = analyse_pull_request_status(today)
    ref_status = analyse_activity_refs(today)
    patch_status = analyse_vulnerability_patch_recommendations(today)
    team_status = analyse_team_membership(today)

    # build page template data
    build_route_data(today)

    # update current audit in history
    history.current = today
    history.alltime[today] = "complete"
    update_history(history)
    # todo - set enabled mode
    return True


@app.cli.command("run-task")
@click.argument("task")
def cli_task(task):
    today = datetime.date.today().isoformat()
    org = config.get_value("github_org")
    history = get_history()

    if task == "repository-status":
        get_github_repositories_and_classify_by_status(org, today)
    elif task == "get-activity":
        get_github_activity_refs_audit(org, today)
        get_github_activity_prs_audit(org, today)
    elif task == "dependabot":
        get_dependabot_status(org, today)
    elif task == "advisories":
        if history.current:
            update_github_advisories_status()
        else:
            get_github_resolve_alert_status()
    elif task == "membership":
        analyse_repo_ownership(today)
        analyse_team_membership(today)
    elif task == "analyse-activity":
        analyse_pull_request_status(today)
        analyse_activity_refs(today)
    elif task == "patch":
        analyse_vulnerability_patch_recommendations(today)
    elif task == "routes":
        build_route_data(today)
    else:
        print("ERROR: Undefined task")


def get_github_resolve_alert_status():
    today = datetime.date.today().isoformat()
    by_alert_status = defaultdict(list)

    repositories = storage.read_json(f"{today}/data/repositories.json")
    for repo in repositories["public"]:
        response = github_rest_client.get(
            f"/repos/{repo.owner.login}/{repo.name}/vulnerability-alerts"
        )
        alerts_enabled = response.status_code == 204
        vulnerable = repo.vulnerabilityAlerts.edges

        if vulnerable:
            status = "vulnerable"
        elif alerts_enabled:
            status = "clean"
        else:
            status = "disabled"

        # append status to repo in original repositories file
        repo.securityAdvisoriesEnabledStatus = alerts_enabled

        by_alert_status[status].append(repo)
        time.sleep(0.2)

    repo_status = storage.save_json(f"{today}/data/repositories.json", repositories)
    status = storage.save_json(f"{today}/data/alert_status.json", by_alert_status)
    return status


def update_github_advisories_status():
    today = datetime.date.today().isoformat()
    current = get_current_audit()

    by_alert_status = defaultdict(list)

    today_repositories = storage.read_json(f"{today}/data/repositories.json")
    current_repositories = storage.read_json(f"{current}/data/repositories.json")
    for today_repo in today_repositories["public"]:
        new_repo = True
        update_status = False
        for current_repo in current_repositories["public"]:
            if today_repo.name == current_repo.name:
                if not current_repo.securityAdvisoriesEnabledStatus:
                    update_status = True
                    new_repo = False

        if new_repo | update_status:

            response = github_rest_client.get(
                f"/repos/{today_repo.owner.login}/{today_repo.name}/vulnerability-alerts"
            )
            alerts_enabled = response.status_code == 204
            vulnerable = today_repo.vulnerabilityAlerts.edges

            if vulnerable:
                status = "vulnerable"
            elif alerts_enabled:
                status = "clean"
            else:
                status = "disabled"

            # append status to repo in original repositories file
            today_repo.securityAdvisoriesEnabledStatus = alerts_enabled

            by_alert_status[status].append(today_repo)
            time.sleep(0.1)
        else:
            alerts_enabled = current_repo.securityAdvisoriesEnabledStatus
            today_repo.securityAdvisoriesEnabledStatus = alerts_enabled

    repo_status = storage.save_json(
        f"{today}/data/repositories.json", today_repositories
    )
    status = storage.save_json(f"{today}/data/alert_status.json", by_alert_status)
    status = storage.save_json(f"{today}/data/alert_status.json", by_alert_status)
    return status


def get_github_repositories_and_classify_by_status(org, today):
    try:
        cursor = None
        last = False
        repository_list = []

        while not last:

            page = pgraph.query("all", org=org, nth=100, after=cursor)

            repository_list.extend(page.organization.repositories.nodes)
            last = not page.organization.repositories.pageInfo.hasNextPage
            cursor = page.organization.repositories.pageInfo.endCursor

        repositories_by_status = repository_summarizer.group_by_status(repository_list)
        save_to = f"{today}/data/repositories.json"
        updated = storage.save_json(save_to, repositories_by_status)

    except Exception as err:
        print(str(err), sys.stderr)
        updated = False
    return updated


def get_github_activity_refs_audit(org, today):
    try:
        cursor = None
        last = False
        repository_list = []

        while not last:

            page = pgraph.query("refs", org=org, nth=50, after=cursor)

            repository_list.extend(page.organization.repositories.nodes)
            last = not page.organization.repositories.pageInfo.hasNextPage
            cursor = page.organization.repositories.pageInfo.endCursor

        total = len(repository_list)
        print(f"Repository list count: {total}", sys.stderr)

        repository_lookup = {repo.name: repo for repo in repository_list}

        total = len(repository_lookup.keys())
        print(f"Repository lookup count: {total}", sys.stderr)

        updated = storage.save_json(
            f"{today}/data/activity_refs.json", repository_lookup
        )

    except Exception as err:
        print("Failed to run activity GQL: " + str(err), sys.stderr)
        updated = False
    return updated


def get_github_activity_prs_audit(org, today):
    try:
        cursor = None
        last = False
        repository_list = []

        while not last:

            page = pgraph.query("prs", org=org, nth=100, after=cursor)

            repository_list.extend(page.organization.repositories.nodes)
            last = not page.organization.repositories.pageInfo.hasNextPage
            cursor = page.organization.repositories.pageInfo.endCursor

        total = len(repository_list)
        print(f"Repository list count: {total}", sys.stderr)

        repository_lookup = {repo.name: repo for repo in repository_list}

        total = len(repository_lookup.keys())
        print(f"Repository lookup count: {total}", sys.stderr)

        updated = storage.save_json(
            f"{today}/data/activity_prs.json", repository_lookup
        )

    except Exception as err:
        print("Failed to run activity GQL: " + str(err), sys.stderr)
        updated = False
    return updated


def get_dependabot_status(org, today):
    try:
        updated = False
        dependabot_status = dependabot_api.get_repos_by_status(org)
        counts = stats.count_types(dependabot_status)
        output = Dict()
        output.counts = counts
        output.repositories = dependabot_status

        raw_data_saved = storage.save_json(
            f"{today}/data/dependabot_status.json", output
        )

        repositories = storage.read_json(f"{today}/data/repositories.json")
        for repo in repositories["public"]:
            for status, dbot_repositories in dependabot_status.items():
                for dbot_repo in dbot_repositories:
                    if dbot_repo.attributes.name == repo.name:
                        repo.dependabotEnabledStatus = status == "active"

        updated = storage.save_json(f"{today}/data/repositories.json", repositories)

    except Exception:
        updated = False

    return updated


def analyse_repo_ownership(today):
    list_owners = defaultdict(list)
    list_topics = defaultdict(list)
    repositories = storage.read_json(f"{today}/data/repositories.json")
    for repo in repositories["public"]:

        if repo.repositoryTopics.edges:
            for topics in repo.repositoryTopics.edges:
                list_owners[repo.name].append(topics.node.topic.name)
                list_topics[topics.node.topic.name].append(repo.name)

    owner_status = storage.save_json(f"{today}/data/owners.json", list_owners)
    topic_status = storage.save_json(f"{today}/data/topics.json", list_topics)
    return owner_status and topic_status


def analyse_pull_request_status(today):
    now = arrow.utcnow()
    pull_requests = storage.read_json(f"{today}/data/activity_prs.json")
    repositories = storage.read_json(f"{today}/data/repositories.json")
    for repo in repositories.public:

        repo_prs = pull_requests[repo.name]

        pr_final_status = "No pull requests in this repository"

        if repo_prs.pullRequests.edges:
            node = repo_prs.pullRequests.edges[0].node

            if node.merged:
                reference_date = arrow.get(node.mergedAt)
                pr_status = "merged"
            elif node.closed:
                reference_date = arrow.get(node.closedAt)
                pr_status = "closed"
            else:
                reference_date = arrow.get(node.createdAt)
                pr_status = "open"

            if reference_date < now.shift(years=-1):
                pr_final_status = (
                    f"Last pull request more than a year ago ({pr_status})"
                )
            elif reference_date < now.shift(months=-1):
                pr_final_status = (
                    f"Last pull request more than a month ago ({pr_status})"
                )
            elif reference_date < now.shift(weeks=-1):
                pr_final_status = (
                    f"Last pull request more than a week ago ({pr_status})"
                )
            else:
                pr_final_status = f"Last pull request this week ({pr_status})"

        repo.recentPullRequestStatus = pr_final_status

    status = storage.save_json(f"{today}/data/repositories.json", repositories)
    return status


def analyse_activity_refs(today):
    now = arrow.utcnow()
    refs = storage.read_json(f"{today}/data/activity_refs.json")
    repositories = storage.read_json(f"{today}/data/repositories.json")
    repo_activity = {}
    for repo_name, repo in refs.items():
        repo_commit_dates = []
        for ref in repo.refs.edges:
            branch = ref.node
            for commit_edge in branch.target.history.edges:
                commit = commit_edge.node
                committed_date = arrow.get(commit.committedDate)
                repo_commit_dates.append(committed_date)
        repo_commit_dates.sort()
        if len(repo_commit_dates) > 0:
            first_commit = repo_commit_dates[0]
            last_commit = repo_commit_dates[-1]
            delta = last_commit - first_commit
            average_delta = delta / len(repo_commit_dates)
            average_days = average_delta.days
            currency_delta = now - last_commit
            currency_days = currency_delta.days

            for status, repo_list in repositories.items():
                if status in ["public","private"]:
                    for repo in repo_list:
                        if repo.name == repo_name:
                            repo.recentCommitDaysAgo = currency_days
                            repo.averageCommitFrequency = average_days
                            repo.isActive = currency_days < 365 and average_days < 180

                            currency_band = "older"
                            if currency_days <= 28:
                                currency_band = "within a month"
                            elif currency_days <= 91:
                                currency_band = "within a quarter"
                            elif currency_days <= 365:
                                currency_band = "within a year"

                            repo.currencyBand = currency_band

    updated = storage.save_json(f"{today}/data/repositories.json", repositories)

    return updated


def analyse_team_membership(today):

    try:
        topics = storage.read_json(f"{today}/data/topics.json")
        repositories = storage.read_json(f"{today}/data/repositories.json")

        # This one can't currently use storage because it's
        # outside the output folder and not written to S3.
        with open("teams.json", "r") as teams_file:
            teams = json.loads(teams_file.read())

        for status, repo_list in repositories.items():
            for repo in repo_list:
                repo_team = "unknown"
                repo_topics = []
                if repo.repositoryTopics:
                    repo_topics = [
                        topic_edge.node.topic.name
                        for topic_edge in repo.repositoryTopics.edges
                    ]

                for team, team_topics in teams.items():
                    for topic in team_topics:
                        if topic in repo_topics:
                            repo_team = team
                repo.team = repo_team

        updated = storage.save_json(f"{today}/data/repositories.json", repositories)
    except Exception as err:
        print(str(err))
        updated = False
    return updated


def analyse_vulnerability_patch_recommendations(today):
    repositories = storage.read_json(f"{today}/data/repositories.json")

    severities = vulnerability_summarizer.SEVERITIES

    vulnerable_list = [
        node for node in repositories.public if node.vulnerabilityAlerts.edges
    ]
    vulnerable_by_severity = vulnerability_summarizer.group_by_severity(vulnerable_list)

    saved = storage.save_json(
        f"{today}/data/vulnerable_by_severity.json", vulnerable_by_severity
    )

    for repo in repositories.public:
        for severity, vuln_repos in vulnerable_by_severity.items():
            for vuln_repo in vuln_repos:
                if repo.name == vuln_repo.name:
                    print(repo.name)
                    repo.maxSeverity = severity
                    repo.patches = vulnerability_summarizer.get_patch_list(repo)
                    repo.vulnerabiltyCounts = vulnerability_summarizer.get_repository_severity_counts(repo)

    updated = storage.save_json(f"{today}/data/repositories.json", repositories)
    return updated


def build_route_data(today):
    route_data_overview_repositories_by_status(today)
    route_data_overview_alert_status(today)
    route_data_overview_vulnerable_repositories(today)


def route_data_overview_repositories_by_status(today):
    repositories_by_status = storage.read_json(f"{today}/data/repositories.json")
    status_counts = stats.count_types(repositories_by_status)
    repo_count = sum(status_counts.values())

    vulnerable_by_severity = storage.read_json(
        f"{today}/data/vulnerable_by_severity.json"
    )
    severity_counts = stats.count_types(vulnerable_by_severity)
    vulnerable_count = sum(severity_counts.values())
    severities = vulnerability_summarizer.SEVERITIES

    template_data = {
        "content": {
            "title": "Overview - Repositories by status",
            "org": config.get_value("github_org"),
            "repositories": {
                "all": repo_count,
                "by_status": status_counts,
                "repos_by_status": repositories_by_status,
            },
            "vulnerable_by_severity": vulnerable_by_severity,
        },
        "footer": {"updated": today},
    }

    overview_status = storage.save_json(
        f"{today}/routes/overview_repositories_by_status.json", template_data
    )
    return overview_status


def route_data_overview_alert_status(today):

    by_alert_status = {"public": {}}
    alert_statuses = storage.read_json(f"{today}/data/alert_status.json")
    for status, repos in alert_statuses.items():
        by_alert_status["public"][status] = len(repos)

    status = storage.save_json(
        f"{today}/routes/count_alert_status.json", by_alert_status
    )
    return status


def route_data_overview_vulnerable_repositories(today):

    alert_status = storage.read_json(f"{today}/routes/count_alert_status.json")

    vulnerable_by_severity = storage.read_json(
        f"{today}/data/vulnerable_by_severity.json"
    )
    severity_counts = stats.count_types(vulnerable_by_severity)
    vulnerable_count = sum(severity_counts.values())
    severities = vulnerability_summarizer.SEVERITIES

    template_data = {
        "content": {
            "title": "Overview - Vulnerable repositories",
            "org": config.get_value("github_org"),
            "vulnerable": {
                "severities": severities,
                "all": vulnerable_count,
                "by_severity": severity_counts,
                "repositories": vulnerable_by_severity,
            },
            "alert_status": alert_status,
        },
        "footer": {"updated": today},
    }

    template_status = storage.save_json(
        f"{today}/routes/overview_vulnerable_repositories.json", template_data
    )
    return template_status


def route_data_overview_activity(today):
    repositories_by_status = storage.read_json(f"{today}/data/repositories.json")
    bands = defaultdict(int)
    repositories_by_activity = defaultdict(list)
    for status, repo_list in repositories_by_status.items():
        if status in ["public","private"]:
            for repo in repo_list:
                if "recentCommitDaysAgo" in repo:
                    currency = repo.currencyBand
                    bands[currency] += 1
                    repositories_by_activity[currency].append(repo)


    template_data = {
        "content": {
            "title": "Overview - Activity",
            "org": config.get_value("github_org"),
            "activity": {
                "counts": bands,
                "repositories": repositories_by_activity
            }
        },
        "footer": {
            "updated": today
        }
    }

    overview_activity_status = storage.save_json(f"{today}/routes/overview_activity.json", template_data)
    return overview_activity_status


def get_header():
    org = config.get_value("github_org")
    org_name = org.title()
    return {"org": org, "app_name": f"{org_name} Audit", "route": request.path}


def get_error_data(message):
    return {"header": get_header(), "content": {"title": message}, "message": message}


@app.route("/")
def route_home():
    try:
        # today = datetime.date.today().isoformat()
        today = get_current_audit()

        content = {"title": "Introduction", "org": config.get_value("github_org")}
        footer = {"updated": today}
        return render_template(
            "pages/home.html", header=get_header(), content=content, footer=footer
        )
    except FileNotFoundError as err:
        return render_template(
            "pages/error.html", **get_error_data("Something went wrong.")
        )


@app.route("/overview")
def route_overview():
    try:
        # today = datetime.date.today().isoformat()
        today = get_current_audit()
        content = {"title": "Overview"}
        footer = {"updated": today}
        repo_stats = storage.read_json(f"{today}/routes/overview.json")
        return render_template(
            "pages/overview.html",
            header=get_header(),
            content=content,
            footer=footer,
            data=repo_stats,
        )
    except FileNotFoundError as err:
        return render_template(
            "pages/error.html", **get_error_data("Something went wrong.")
        )


@app.route("/overview/repository-status")
def route_overview_repository_status():
    try:
        current = get_current_audit()
        template_data = storage.read_json(
            f"{current}/routes/overview_repositories_by_status.json"
        )
        return render_template(
            "pages/overview_repository_status.html",
            header=get_header(),
            **template_data,
        )
    except FileNotFoundError as err:
        return render_template(
            "pages/error.html", **get_error_data("Something went wrong.")
        )


@app.route("/overview/vulnerable-repositories")
def route_overview_vulnerable_repositories():
    try:
        current = get_current_audit()
        template_data = storage.read_json(
            f"{current}/routes/overview_vulnerable_repositories.json"
        )
        return render_template(
            "pages/overview_vulnerable_repositories.html",
            header=get_header(),
            **template_data,
        )
    except FileNotFoundError as err:
        return render_template("pages/error.html", **get_error_data("File not found."))


@app.route("/overview/repository-monitoring-status")
def route_overview_repository_monitoring_status():
    try:
        # today = datetime.date.today().isoformat()
        today = get_current_audit()
        content = {"title": "Overview - Alert status"}
        footer = {"updated": today}

        alert_status = storage.read_json(f"{today}/routes/count_alert_status.json")
        return render_template(
            "pages/overview_repository_monitoring_status.html",
            header=get_header(),
            content=content,
            footer=footer,
            data=alert_status,
        )
    except FileNotFoundError as err:
        return render_template(
            "pages/error.html", **get_error_data("Something went wrong.")
        )


def route_overview_activity():
    try:
        current = get_current_audit()
        template_data = storage.read_json(f"{current}/routes/overview_activity.json")
        return render_template(
            "pages/overview_activity.html",
            header=get_header(),
            **template_data
        )
    except FileNotFoundError as err:
        return render_template(
            "pages/error.html", **get_error_data("File not found.")
        )


@app.route("/by-repository")
def route_by_repository():
    try:
        # today = datetime.date.today().isoformat()
        today = get_current_audit()
        content = {"title": "By repository"}
        footer = {"updated": today}
        topics = storage.read_json(f"{today}/data/topics.json")
        raw_repositories = storage.read_json(f"{today}/data/repositories.json")
        repositories = {repo.name: repo for repo in raw_repositories["public"]}

        team_dict = defaultdict(list)
        for repo_name, repo in repositories.items():
            team_dict[repo.team].append(repo_name)

        teams = list(team_dict.keys())
        teams.sort()

        other_topics = {
            topic_name: topics[topic_name] for topic_name in set(topics.keys())
        }

        total = len([*repositories.keys()])
        print(f"Count of repos in lookup: {total}", sys.stderr)

        return render_template(
            "pages/by_repository.html",
            header=get_header(),
            content=content,
            footer=footer,
            other_topics=other_topics,
            teams=teams,
            team_dict=team_dict,
            repositories=repositories,
        )
    except FileNotFoundError as err:
        return render_template(
            "pages/error.html", **get_error_data("Something went wrong.")
        )
