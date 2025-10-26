
"""
GitHub Profile Stats Generator (fixed)
Author: Sudaraka Senevirathne, 2025

- Fetches GitHub stats (GraphQL) with caching
- Updates SVGs that follow the SAG structure (ids: commit_data, star_data, repo_data,
  contrib_data, follower_data, loc_data, loc_add, loc_del, age_data and matching *_dots).
- Optionally injects/updates an <image id="avatar_image"> node with your photo.
"""

import datetime
import os
import time
import hashlib
import json
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from dateutil import relativedelta
import requests
from lxml import etree


# ----------------------------- Config ---------------------------------

@dataclass
class Config:
    user_name: str
    access_token: str
    birthday: datetime.datetime
    linkedin_url: str = "https://lk.linkedin.com/in/sudarakas"
    twitter_url: str = "https://twitter.com/sudarakase"
    cache_dir: Path = Path('cache')
    comment_lines: int = 7
    dark_svg: Path = Path('/mnt/data/dark_mode.svg')
    light_svg: Path = Path('/mnt/data/light_mode.svg')
    avatar_path: Optional[Path] = Path('/mnt/data/1758076803612.jpg')

    @classmethod
    def from_env(cls):
        token = os.environ.get('ACCESS_TOKEN') or os.environ.get('GITHUB_TOKEN')
        if not token:
            raise RuntimeError("Missing ACCESS_TOKEN/GITHUB_TOKEN environment variable.")
        return cls(
            user_name=os.environ.get('USER_NAME', 'sudarakas'),
            access_token=token,
            birthday=datetime.datetime(1996, 9, 5)
        )


# ------------------------------ API -----------------------------------

class GitHubAPIClient:
    def __init__(self, config: Config):
        self.config = config
        self.headers = {'authorization': f'bearer {config.access_token}'}
        self.base_url = 'https://api.github.com/graphql'
        self.query_count = {}
        self.owner_id = None

    def _increment_count(self, query_name: str):
        self.query_count[query_name] = self.query_count.get(query_name, 0) + 1

    def _query(self, query_name: str, query: str, variables: Dict) -> Dict:
        self._increment_count(query_name)
        try:
            r = requests.post(self.base_url, json={'query': query, 'variables': variables},
                              headers=self.headers, timeout=30)
            r.raise_for_status()
            data = r.json()
            if 'errors' in data:
                raise RuntimeError(f"{query_name}: {data['errors']}")
            return data
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"{query_name} failed: {e}")

    def get_user_data(self) -> Tuple[Dict, Dict]:
        q = '''
        query($login: String!) {
          user(login: $login) {
            id
            createdAt
            followers { totalCount }
            repositories(ownerAffiliations: OWNER) { totalCount }
          }
        }'''
        d = self._query('user_data', q, {'login': self.config.user_name})
        user = d['data']['user']
        self.owner_id = {'id': user['id']}
        return self.owner_id, {
            'created_at': user['createdAt'],
            'followers': user['followers']['totalCount'],
            'owner_repo_count': user['repositories']['totalCount'],
        }

    def get_repositories(self, owner_affiliation: List[str], cursor: Optional[str] = None) -> Dict:
        q = '''
        query($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
          user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation, orderBy: {field: UPDATED_AT, direction: DESC}) {
              totalCount
              edges {
                node {
                  nameWithOwner
                  stargazers { totalCount }
                  defaultBranchRef {
                    target {
                      ... on Commit {
                        history { totalCount }
                      }
                    }
                  }
                }
              }
              pageInfo { endCursor hasNextPage }
            }
          }
        }'''
        return self._query('repositories', q, {
            'owner_affiliation': owner_affiliation,
            'login': self.config.user_name,
            'cursor': cursor
        })

    def get_repo_commits(self, owner: str, repo_name: str, cursor: Optional[str] = None) -> Dict:
        q = '''
        query($repo_name: String!, $owner: String!, $cursor: String) {
          repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
              target {
                ... on Commit {
                  history(first: 100, after: $cursor) {
                    totalCount
                    edges {
                      node {
                        committedDate
                        author { user { id } }
                        deletions
                        additions
                      }
                    }
                    pageInfo { endCursor hasNextPage }
                  }
                }
              }
            }
          }
        }'''
        return self._query('repo_commits', q, {'repo_name': repo_name, 'owner': owner, 'cursor': cursor})


# --------------------------- Calculation -------------------------------

class CacheManager:
    def __init__(self, config: Config):
        self.cache_dir = config.cache_dir
        self.cache_dir.mkdir(exist_ok=True)
        self.cache_file = self.cache_dir / f"{hashlib.sha256(config.user_name.encode()).hexdigest()}.json"

    def load(self) -> Dict:
        if not self.cache_file.exists():
            return {}
        try:
            return json.loads(self.cache_file.read_text())
        except Exception:
            return {}

    def save(self, data: Dict):
        self.cache_file.write_text(json.dumps(data, indent=2))

    def calc_loc(self, edges: List[Dict], api: GitHubAPIClient) -> Dict:
        cache = self.load()
        added = deleted = commits = 0

        for e in edges:
            n = e['node']
            if not n['defaultBranchRef']:
                continue
            repo_name = n['nameWithOwner']
            commit_count = n['defaultBranchRef']['target']['history']['totalCount']
            key = hashlib.sha256(repo_name.encode()).hexdigest()

            if key in cache and cache[key].get('commit_count') == commit_count:
                s = cache[key]
                added += s['added']; deleted += s['deleted']; commits += s['commits']
                continue

            owner, repo = repo_name.split('/')
            s = self._calc_repo_loc(api, owner, repo)
            cache[key] = {
                'repo_name': repo_name,
                'commit_count': commit_count,
                'added': s['added'],
                'deleted': s['deleted'],
                'commits': s['commits']
            }
            added += s['added']; deleted += s['deleted']; commits += s['commits']

        self.save(cache)
        return {'added': added, 'deleted': deleted, 'total': added - deleted, 'commits': commits}

    def _calc_repo_loc(self, api: GitHubAPIClient, owner: str, repo: str) -> Dict:
        a = d = c = 0
        cursor = None
        while True:
            data = api.get_repo_commits(owner, repo, cursor)
            ref = data['data']['repository']['defaultBranchRef']
            if not ref:
                break
            hist = ref['target']['history']
            for edge in hist['edges']:
                node = edge['node']
                if node['author']['user'] and node['author']['user']['id'] == api.owner_id['id']:
                    c += 1; a += node['additions']; d += node['deletions']
            if not hist['pageInfo']['hasNextPage']:
                break
            cursor = hist['pageInfo']['endCursor']
        return {'added': a, 'deleted': d, 'commits': c}


class StatsCalculator:
    def __init__(self, config: Config, api: GitHubAPIClient):
        self.config = config
        self.api = api
        self.cache = CacheManager(config)

    def calculate_age(self) -> str:
        diff = relativedelta.relativedelta(datetime.datetime.today(), self.config.birthday)
        def s(n): return '' if n == 1 else 's'
        bday = ' 🎂' if (diff.months == 0 and diff.days == 0) else ''
        return f"{diff.years} year{s(diff.years)}, {diff.months} month{s(diff.months)}, {diff.days} day{s(diff.days)}{bday}"

    def repo_stats(self, aff: List[str]) -> Dict:
        all_edges, cursor = [], None
        total_repos = total_stars = 0
        while True:
            data = self.api.get_repositories(aff, cursor)
            repos = data['data']['user']['repositories']
            total_repos = repos['totalCount']
            edges = repos['edges']
            all_edges.extend(edges)
            for e in edges:
                total_stars += e['node']['stargazers']['totalCount']
            if not repos['pageInfo']['hasNextPage']:
                break
            cursor = repos['pageInfo']['endCursor']

        loc = self.cache.calc_loc(all_edges, self.api)
        return {
            'total_repos': total_repos,
            'total_stars': total_stars,
            'loc_added': loc['added'],
            'loc_deleted': loc['deleted'],
            'loc_total': loc['total'],
            'total_commits': loc['commits']
        }


# ----------------------------- SVG ------------------------------------

class SVGGenerator:
    @staticmethod
    def _fmt(n):
        return f"{n:,}" if isinstance(n, int) else str(n)

    @staticmethod
    def _update_with_dots(root, element_id: str, value, justify_length: int = 0):
        val = SVGGenerator._fmt(value)
        el = root.find(f".//*[@id='{element_id}']")
        if el is not None:
            el.text = val
        if justify_length > 0:
            dots = root.find(f".//*[@id='{element_id}_dots']")
            if dots is not None:
                pad = max(0, justify_length - len(val))
                if pad == 0:
                    dots.text = ''
                elif pad == 1:
                    dots.text = ' '
                elif pad == 2:
                    dots.text = '. '
                else:
                    dots.text = ' ' + ('.' * pad) + ' '

    @staticmethod
    def _ensure_avatar(root, image_path: Path):
        if not image_path or not image_path.exists():
            return
        # Try to find existing node
        img = root.find(".//*[@id='avatar_image']")
        if img is None:
            # Create a small round avatar near the right column title
            defs = root.find('defs')
            if defs is None:
                defs = etree.SubElement(root, 'defs')
            clip = root.find(".//*[@id='avatar_clip']")
            if clip is None:
                clip = etree.SubElement(defs, 'clipPath', id='avatar_clip')
                etree.SubElement(clip, 'circle', cx='360', cy='28', r='20')
            img = etree.SubElement(root, 'image', id='avatar_image', x='340', y='8', width='40', height='40')
            img.set('clip-path', 'url(#avatar_clip)')
        # Set href (handle both xlink and href)
        img.set('{http://www.w3.org/1999/xlink}href', str(image_path))
        img.set('href', str(image_path))

    @staticmethod
    def update_svg(svg_path: Path, stats: Dict, avatar_path: Optional[Path] = None):
        tree = etree.parse(str(svg_path))
        root = tree.getroot()

        # Main numeric fields
        SVGGenerator._update_with_dots(root, 'commit_data', stats['commits'], 0)
        SVGGenerator._update_with_dots(root, 'star_data', stats['stars'], 0)
        SVGGenerator._update_with_dots(root, 'repo_data', stats['repos'], 0)
        SVGGenerator._update_with_dots(root, 'contrib_data', stats['contrib_repos'], 0)
        SVGGenerator._update_with_dots(root, 'follower_data', stats['followers'], 0)
        SVGGenerator._update_with_dots(root, 'loc_data', stats['loc_total'], 0)
        SVGGenerator._update_with_dots(root, 'loc_add', stats['loc_added'], 0)
        SVGGenerator._update_with_dots(root, 'loc_del', stats['loc_deleted'], 0)
        SVGGenerator._update_with_dots(root, 'age_data', stats.get('age', ''), 0)

        # Optional avatar
        if avatar_path:
            SVGGenerator._ensure_avatar(root, avatar_path)

        tree.write(str(svg_path), encoding='utf-8', xml_declaration=True)


# ----------------------------- Main -----------------------------------

def main():
    print("GitHub Stats Generator (SAG)")
    config = Config.from_env()
    api = GitHubAPIClient(config)
    calc = StatsCalculator(config, api)

    _owner_id, user_meta = api.get_user_data()
    age = calc.calculate_age()
    owner = calc.repo_stats(['OWNER'])
    contrib = calc.repo_stats(['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER'])

    final_stats = {
        'commits': owner['total_commits'],
        'stars': owner['total_stars'],
        'repos': owner['total_repos'],
        'contrib_repos': contrib['total_repos'],
        'followers': user_meta['followers'],
        'loc_added': owner['loc_added'],
        'loc_deleted': owner['loc_deleted'],
        'loc_total': owner['loc_total'],
        'age': age
    }

    SVGGenerator.update_svg(config.dark_svg, final_stats, config.avatar_path)
    SVGGenerator.update_svg(config.light_svg, final_stats, config.avatar_path)
    print("SVGs updated.")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Cancelled by user.")
    except Exception as e:
        print(f"Error: {e}")
        raise
