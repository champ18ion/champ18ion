import datetime
import hashlib
import json
import os

import requests
from dateutil import relativedelta

HEADERS = {'authorization': 'token ' + os.environ['ACCESS_TOKEN']}
USER_NAME = os.environ['USER_NAME']
CACHE_PATH = os.path.join(os.path.dirname(__file__), 'cache', f'{USER_NAME}.json')
QUERY_COUNT = {'user_getter': 0, 'follower_getter': 0, 'graph_repos_stars': 0,
               'recursive_loc': 0, 'graph_commits': 0, 'loc_query': 0}


def simple_request(func_name, query, variables):
    request = requests.post('https://api.github.com/graphql',
                             json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code == 200:
        return request
    raise Exception(func_name, 'failed with', request.status_code, request.text, QUERY_COUNT)


def query_count(func_name):
    QUERY_COUNT[func_name] += 1


def user_getter(username):
    """Returns the account id and creation date of the user"""
    query_count('user_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            id
            createdAt
        }
    }'''
    variables = {'login': username}
    request = simple_request(user_getter.__name__, query, variables)
    return {'id': request.json()['data']['user']['id']}, request.json()['data']['user']['createdAt']


def follower_getter(username):
    """Returns the number of followers of the user"""
    query_count('follower_getter')
    query = '''
    query($login: String!){
        user(login: $login) {
            followers {
                totalCount
            }
        }
    }'''
    request = simple_request(follower_getter.__name__, query, {'login': username})
    return int(request.json()['data']['user']['followers']['totalCount'])


def graph_commits(start_date, end_date):
    """Uses the GraphQL v4 API to return total commit count in a given date range"""
    query_count('graph_commits')
    query = '''
    query($start_date: DateTime!, $end_date: DateTime!, $login: String!) {
        user(login: $login) {
            contributionsCollection(from: $start_date, to: $end_date) {
                contributionCalendar {
                    totalContributions
                }
            }
        }
    }'''
    variables = {'start_date': start_date, 'end_date': end_date, 'login': USER_NAME}
    request = simple_request(graph_commits.__name__, query, variables)
    return int(request.json()['data']['user']['contributionsCollection']['contributionCalendar']['totalContributions'])


def total_commits(created_at):
    """Sums commit contributions year by year since account creation (API caps a single query at 1 year)"""
    created = datetime.datetime.strptime(created_at, '%Y-%m-%dT%H:%M:%SZ')
    now = datetime.datetime.utcnow()
    total = 0
    cursor = created
    while cursor < now:
        window_end = min(cursor + relativedelta.relativedelta(years=1), now)
        total += graph_commits(cursor.strftime('%Y-%m-%dT%H:%M:%SZ'), window_end.strftime('%Y-%m-%dT%H:%M:%SZ'))
        cursor = window_end
    return total


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    """Returns total repository count or total star count across owned repos"""
    query_count('graph_repos_stars')
    query = '''
    query($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges {
                    node {
                        nameWithOwner
                        stargazers {
                            totalCount
                        }
                    }
                }
                pageInfo {
                    endCursor
                    hasNextPage
                }
            }
        }
    }'''
    variables = {'owner_affiliation': owner_affiliation, 'login': USER_NAME, 'cursor': cursor}
    request = simple_request(graph_repos_stars.__name__, query, variables)
    repos = request.json()['data']['user']['repositories']
    if count_type == 'repos':
        return repos['totalCount']
    if count_type == 'stars':
        total = sum(edge['node']['stargazers']['totalCount'] for edge in repos['edges'])
        if repos['pageInfo']['hasNextPage']:
            total += graph_repos_stars('stars', owner_affiliation, repos['pageInfo']['endCursor'])
        return total


def contributed_repos(username):
    """Returns the number of repositories contributed to (excluding own)"""
    query_count('graph_repos_stars')
    query = '''
    query($login: String!) {
        user(login: $login) {
            repositoriesContributedTo(first: 1, contributionTypes: [COMMIT]) {
                totalCount
            }
        }
    }'''
    request = simple_request(contributed_repos.__name__, query, {'login': username})
    return int(request.json()['data']['user']['repositoriesContributedTo']['totalCount'])


def owned_repo_names(cursor=None, repos=None):
    """Returns list of (name, owner, defaultBranch commit count) for all owned repos"""
    query_count('graph_repos_stars')
    if repos is None:
        repos = []
    query = '''
    query($login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: [OWNER], isFork: false) {
                edges {
                    node {
                        name
                        owner { login }
                        defaultBranchRef {
                            target {
                                ... on Commit { history { totalCount } }
                            }
                        }
                    }
                }
                pageInfo { endCursor hasNextPage }
            }
        }
    }'''
    request = simple_request(owned_repo_names.__name__, query, {'login': USER_NAME, 'cursor': cursor})
    data = request.json()['data']['user']['repositories']
    for edge in data['edges']:
        node = edge['node']
        commit_count = 0
        if node['defaultBranchRef']:
            commit_count = node['defaultBranchRef']['target']['history']['totalCount']
        repos.append({'name': node['name'], 'owner': node['owner']['login'], 'commit_count': commit_count})
    if data['pageInfo']['hasNextPage']:
        return owned_repo_names(data['pageInfo']['endCursor'], repos)
    return repos


def recursive_loc(owner, repo_name, user_id, cursor=None, additions=0, deletions=0, my_commits=0):
    """Paginates a repo's default-branch commit history, summing additions/deletions authored by user_id"""
    query_count('recursive_loc')
    query = '''
    query($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            edges {
                                node {
                                    author { user { id } }
                                    additions
                                    deletions
                                }
                            }
                            pageInfo { endCursor hasNextPage }
                        }
                    }
                }
            }
        }
    }'''
    variables = {'repo_name': repo_name, 'owner': owner, 'cursor': cursor}
    request = requests.post('https://api.github.com/graphql',
                             json={'query': query, 'variables': variables}, headers=HEADERS)
    if request.status_code != 200:
        raise Exception('recursive_loc failed with', request.status_code, request.text, QUERY_COUNT)
    branch = request.json()['data']['repository']['defaultBranchRef']
    if branch is None:
        return 0, 0, 0
    history = branch['target']['history']
    for edge in history['edges']:
        node = edge['node']
        if node['author']['user'] and node['author']['user']['id'] == user_id:
            additions += node['additions']
            deletions += node['deletions']
            my_commits += 1
    if history['pageInfo']['hasNextPage']:
        return recursive_loc(owner, repo_name, user_id, history['pageInfo']['endCursor'], additions, deletions, my_commits)
    return additions, deletions, my_commits


def load_cache():
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, 'r') as f:
            return json.load(f)
    return {}


def save_cache(cache):
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, 'w') as f:
        json.dump(cache, f, indent=2)


def loc_counter(user_id):
    """Sums lines added/deleted across all owned, non-fork repos. Skips repos whose commit
    count hasn't changed since the last run (cached), so re-runs stay cheap."""
    cache = load_cache()
    total_add, total_del = 0, 0
    for repo in owned_repo_names():
        key = f"{repo['owner']}/{repo['name']}"
        cached = cache.get(key)
        if cached and cached['commit_count'] == repo['commit_count']:
            total_add += cached['additions']
            total_del += cached['deletions']
            continue
        additions, deletions, _ = recursive_loc(repo['owner'], repo['name'], user_id)
        cache[key] = {'commit_count': repo['commit_count'], 'additions': additions, 'deletions': deletions}
        total_add += additions
        total_del += deletions
    save_cache(cache)
    return total_add, total_del


def format_number(num):
    return '{:,}'.format(num)


def daily_readme(created_at):
    born = datetime.datetime.strptime(created_at, '%Y-%m-%dT%H:%M:%SZ')
    diff = relativedelta.relativedelta(datetime.datetime.utcnow(), born)
    return '{} year{}, {} month{}, {} day{}'.format(
        diff.years, 's' if diff.years != 1 else '',
        diff.months, 's' if diff.months != 1 else '',
        diff.days, 's' if diff.days != 1 else '')


def render_svg(template_path, output_path, values):
    with open(template_path, 'r', encoding='utf-8') as f:
        svg = f.read()
    for key, value in values.items():
        svg = svg.replace('{{' + key + '}}', str(value))
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(svg)


def main():
    user, created_at = user_getter(USER_NAME)
    user_id = user['id']

    repos = graph_repos_stars('repos', ['OWNER'])
    stars = graph_repos_stars('stars', ['OWNER'])
    followers = follower_getter(USER_NAME)
    contributed = contributed_repos(USER_NAME)
    commits = total_commits(created_at)
    additions, deletions = loc_counter(user_id)
    net_loc = additions - deletions

    values = {
        'repos': format_number(repos),
        'contributed': format_number(contributed),
        'stars': format_number(stars),
        'commits': format_number(commits),
        'followers': format_number(followers),
        'loc': format_number(net_loc),
        'loc_add': format_number(additions),
        'loc_del': format_number(deletions),
        'age': daily_readme(created_at),
    }

    base_dir = os.path.dirname(__file__)
    render_svg(os.path.join(base_dir, 'dark_mode.svg.tmpl'), os.path.join(base_dir, 'dark_mode.svg'), values)
    render_svg(os.path.join(base_dir, 'light_mode.svg.tmpl'), os.path.join(base_dir, 'light_mode.svg'), values)

    print('Wrote dark_mode.svg and light_mode.svg')
    print('API calls this run:', QUERY_COUNT)


if __name__ == '__main__':
    main()
