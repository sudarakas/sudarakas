"""
GitHub Profile Stats Generator
Sudaraka Senevirathne, 2025
Fetches and displays GitHub statistics with caching
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


@dataclass
class Config:
    """Configuration settings"""
    user_name: str
    access_token: str
    birthday: datetime.datetime
    linkedin_url: str = "https://lk.linkedin.com/in/sudarakas"
    twitter_url: str = "https://twitter.com/sudarakase"
    cache_dir: Path = Path('cache')
    comment_lines: int = 7
    
    @classmethod
    def from_env(cls):
        """Load configuration from environment variables"""
        return cls(
            user_name=os.environ.get('USER_NAME', 'sudarakas'),
            access_token=os.environ['ACCESS_TOKEN'],
            birthday=datetime.datetime(1996, 9, 5)
        )


class GitHubAPIClient:
    """Execute GraphQL query with error handling"""
    
    def __init__(self, config):
        self.config = config
        self.headers = {'authorization': f'token {config.access_token}'}
        self.base_url = 'https://api.github.com/graphql'
        self.query_count = {}
        self.owner_id = None
        
        # Retry configuration
        self.max_retries = 5
        self.base_delay = 2  # seconds
        self.max_delay = 60  # seconds
        
    def _query(self, query_name: str, query: str, variables: Dict) -> Dict:
        """Execute GraphQL query with error handling"""
        self._increment_count(query_name)
        
        for attempt in range(self.max_retries):
            try:
                response = requests.post(
                    self.base_url,
                    json={'query': query, 'variables': variables},
                    headers=self.headers,
                    timeout=30
                )
                
                # Handle rate limiting (403) and server errors (5xx)
                if response.status_code == 403:
                    # Check if rate limited
                    rate_limit_remaining = response.headers.get('X-RateLimit-Remaining', '0')
                    if rate_limit_remaining == '0':
                        reset_time = int(response.headers.get('X-RateLimit-Reset', 0))
                        wait_time = max(reset_time - time.time(), 0) + 5
                        print(f'Rate limited. Waiting {wait_time:.0f}s...')
                        time.sleep(wait_time)
                        continue
                
                # Retry on server errors (502, 503, 504, etc.)
                if response.status_code >= 500:
                    if attempt < self.max_retries - 1:
                        delay = min(self.base_delay * (2 ** attempt), self.max_delay)
                        print(f'Server error {response.status_code} on {query_name}. '
                              f'Retrying in {delay:.1f}s... (attempt {attempt + 1}/{self.max_retries})')
                        time.sleep(delay)
                        continue
                
                # Raise for other HTTP errors
                response.raise_for_status()
                
                # Parse response
                data = response.json()
                if 'errors' in data:
                    # Check if it's a timeout error that we can retry
                    error_msg = str(data['errors'])
                    if 'timeout' in error_msg.lower() and attempt < self.max_retries - 1:
                        delay = min(self.base_delay * (2 ** attempt), self.max_delay)
                        print(f'GraphQL timeout on {query_name}. Retrying in {delay:.1f}s...')
                        time.sleep(delay)
                        continue
                    raise Exception(f"GraphQL errors: {data['errors']}")
                
                return data
                
            except requests.exceptions.Timeout:
                if attempt < self.max_retries - 1:
                    delay = min(self.base_delay * (2 ** attempt), self.max_delay)
                    print(f'Request timeout on {query_name}. Retrying in {delay:.1f}s...')
                    time.sleep(delay)
                    continue
                raise Exception(f"{query_name} timed out after {self.max_retries} attempts")
                
            except requests.exceptions.ConnectionError:
                if attempt < self.max_retries - 1:
                    delay = min(self.base_delay * (2 ** attempt), self.max_delay)
                    print(f'Connection error on {query_name}. Retrying in {delay:.1f}s...')
                    time.sleep(delay)
                    continue
                raise Exception(f"{query_name} connection failed after {self.max_retries} attempts")
                
            except requests.exceptions.RequestException as e:
                # Don't retry client errors (4xx except 403)
                if hasattr(e, 'response') and e.response is not None:
                    if 400 <= e.response.status_code < 500 and e.response.status_code != 403:
                        raise Exception(f"{query_name} failed: {str(e)}")
                
                if attempt < self.max_retries - 1:
                    delay = min(self.base_delay * (2 ** attempt), self.max_delay)
                    print(f'Request failed on {query_name}. Retrying in {delay:.1f}s...')
                    time.sleep(delay)
                    continue
                raise Exception(f"{query_name} failed after {self.max_retries} attempts: {str(e)}")
        
        raise Exception(f"{query_name} failed after {self.max_retries} attempts")
    
    def _increment_count(self, query_name: str):
        """Track API call counts"""
        self.query_count[query_name] = self.query_count.get(query_name, 0) + 1


class StatsCalculator:
    """Calculate various statistics"""
    
    def __init__(self, config: Config, api_client: GitHubAPIClient):
        self.config = config
        self.api = api_client
        self.cache_manager = CacheManager(config)
    
    def calculate_age(self) -> str:
        """Calculate age in years, months, days"""
        diff = relativedelta.relativedelta(datetime.datetime.today(), self.config.birthday)
        
        def plural(n): return 's' if n != 1 else ''
        
        birthday_emoji = ' 🎂' if (diff.months == 0 and diff.days == 0) else ''
        
        return (f"{diff.years} year{plural(diff.years)}, "
                f"{diff.months} month{plural(diff.months)}, "
                f"{diff.days} day{plural(diff.days)}{birthday_emoji}")
    
    def get_repository_stats(self, owner_affiliation: List[str]) -> Dict:
        """Get comprehensive repository statistics"""
        all_edges = []
        cursor = None
        total_repos = 0
        total_stars = 0
        
        while True:
            data = self.api.get_repositories(owner_affiliation, cursor)
            repos = data['data']['user']['repositories']
            
            total_repos = repos['totalCount']
            edges = repos['edges']
            all_edges.extend(edges)
            
            # Count stars
            for edge in edges:
                total_stars += edge['node']['stargazers']['totalCount']
            
            if not repos['pageInfo']['hasNextPage']:
                break
            cursor = repos['pageInfo']['endCursor']
        
        # Calculate LOC
        loc_data = self.cache_manager.calculate_loc(all_edges, self.api)
        
        return {
            'total_repos': total_repos,
            'total_stars': total_stars,
            'loc_added': loc_data['added'],
            'loc_deleted': loc_data['deleted'],
            'loc_total': loc_data['total'],
            'total_commits': loc_data['commits']
        }


class CacheManager:
    """Manages caching of repository data"""
    
    def __init__(self, config: Config):
        self.config = config
        self.cache_dir = config.cache_dir
        self.cache_dir.mkdir(exist_ok=True)
        self.cache_file = self._get_cache_filename()
    
    def _get_cache_filename(self) -> Path:
        """Generate unique cache filename for user"""
        user_hash = hashlib.sha256(self.config.user_name.encode()).hexdigest()
        return self.cache_dir / f"{user_hash}.json"
    
    def load_cache(self) -> Dict:
        """Load cache from file"""
        if not self.cache_file.exists():
            return {}
        
        try:
            with open(self.cache_file, 'r') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    
    def save_cache(self, cache_data: Dict):
        """Save cache to file"""
        with open(self.cache_file, 'w') as f:
            json.dump(cache_data, f, indent=2)
    
    def calculate_loc(self, edges: List[Dict], api: GitHubAPIClient) -> Dict:
        """Calculate lines of code with caching"""
        cache = self.load_cache()
        
        total_added = 0
        total_deleted = 0
        total_commits = 0
        
        for edge in edges:
            node = edge['node']
            repo_name = node['nameWithOwner']
            
            # Skip empty repos
            if not node['defaultBranchRef']:
                continue
            
            commit_count = node['defaultBranchRef']['target']['history']['totalCount']
            repo_hash = hashlib.sha256(repo_name.encode()).hexdigest()
            
            # Check cache
            if repo_hash in cache and cache[repo_hash]['commit_count'] == commit_count:
                stats = cache[repo_hash]
                total_added += stats['added']
                total_deleted += stats['deleted']
                total_commits += stats['commits']
            else:
                # Fetch fresh data
                owner, repo = repo_name.split('/')
                stats = self._calculate_repo_loc(api, owner, repo)
                
                cache[repo_hash] = {
                    'repo_name': repo_name,
                    'commit_count': commit_count,
                    'added': stats['added'],
                    'deleted': stats['deleted'],
                    'commits': stats['commits']
                }
                
                total_added += stats['added']
                total_deleted += stats['deleted']
                total_commits += stats['commits']
        
        self.save_cache(cache)
        
        return {
            'added': total_added,
            'deleted': total_deleted,
            'total': total_added - total_deleted,
            'commits': total_commits
        }
    
    def _calculate_repo_loc(self, api: GitHubAPIClient, owner: str, repo: str) -> Dict:
        """Calculate LOC for a single repository"""
        added = 0
        deleted = 0
        commits = 0
        cursor = None
        
        while True:
            data = api.get_repo_commits(owner, repo, cursor)
            
            if not data['data']['repository']['defaultBranchRef']:
                break
            
            history = data['data']['repository']['defaultBranchRef']['target']['history']
            
            for edge in history['edges']:
                node = edge['node']
                if node['author']['user'] and node['author']['user']['id'] == api.owner_id['id']:
                    commits += 1
                    added += node['additions']
                    deleted += node['deletions']
            
            if not history['pageInfo']['hasNextPage']:
                break
            cursor = history['pageInfo']['endCursor']
        
        return {'added': added, 'deleted': deleted, 'commits': commits}


class SVGGenerator:
    """Generates and updates SVG files"""
    
    @staticmethod
    def update_svg(filename: str, stats: Dict):
        """Update SVG file with statistics"""
        tree = etree.parse(filename)
        root = tree.getroot()
        
        def format_number(n):
            return f"{n:,}" if isinstance(n, int) else str(n)
        
        def update_element(element_id: str, value, justify_length: int = 0):
            formatted_value = format_number(value)
            element = root.find(f".//*[@id='{element_id}']")
            if element is not None:
                element.text = formatted_value
            
            # Update dots for justification
            if justify_length > 0:
                dots_id = f"{element_id}_dots"
                dots_element = root.find(f".//*[@id='{dots_id}']")
                if dots_element is not None:
                    just_len = max(0, justify_length - len(formatted_value))
                    if just_len == 0:
                        dots_element.text = ''
                    elif just_len == 1:
                        dots_element.text = ' '
                    elif just_len == 2:
                        dots_element.text = '. '
                    else:
                        dots_element.text = ' ' + ('.' * just_len) + ' '
        
        # Update all elements
        update_element('commit_data', stats['commits'], 22)
        update_element('star_data', stats['stars'], 14)
        update_element('repo_data', stats['repos'], 6)
        update_element('contrib_data', stats['contrib_repos'])
        update_element('follower_data', stats['followers'], 10)
        update_element('loc_data', stats['loc_total'], 9)
        update_element('loc_add', stats['loc_added'])
        update_element('loc_del', stats['loc_deleted'], 7)
        
        tree.write(filename, encoding='utf-8', xml_declaration=True)


def main():
    """Main execution function"""
    print('GitHub Stats Generator')
    print('=' * 50)
    
    # Load configuration
    config = Config.from_env()
    config.cache_dir.mkdir(exist_ok=True)
    
    # Initialize components
    api = GitHubAPIClient(config)
    calculator = StatsCalculator(config, api)
    
    # Performance tracking
    start_time = time.perf_counter()
    
    print('\nFetching statistics...\n')
    
    # Get user data
    t0 = time.perf_counter()
    owner_id, created_at = api.get_user_data()
    print(f'User data: {(time.perf_counter() - t0)*1000:.2f} ms')
    
    # Calculate age
    t0 = time.perf_counter()
    age = calculator.calculate_age()
    print(f'Age calculation: {(time.perf_counter() - t0)*1000:.2f} ms')
    
    # Get repository stats (owner only)
    t0 = time.perf_counter()
    owner_stats = calculator.get_repository_stats(['OWNER'])
    print(f'Owner repos: {(time.perf_counter() - t0):.2f} s')
    
    # Get contributed repos
    t0 = time.perf_counter()
    contrib_stats = calculator.get_repository_stats(
        ['OWNER', 'COLLABORATOR', 'ORGANIZATION_MEMBER']
    )
    print(f'All repos: {(time.perf_counter() - t0):.2f} s')
    
    # Prepare final stats
    final_stats = {
        'commits': owner_stats['total_commits'],
        'stars': owner_stats['total_stars'],
        'repos': owner_stats['total_repos'],
        'contrib_repos': contrib_stats['total_repos'],
        'followers': 0,  # Will be fetched from user_data
        'loc_added': owner_stats['loc_added'],
        'loc_deleted': owner_stats['loc_deleted'],
        'loc_total': owner_stats['loc_total']
    }
    
    # Update SVG files
    print('\nUpdating SVG files...')
    SVGGenerator.update_svg('dark_mode.svg', final_stats)
    SVGGenerator.update_svg('light_mode.svg', final_stats)
    print('SVG files updated')
    
    # Summary
    total_time = time.perf_counter() - start_time
    print('\n' + '=' * 50)
    print(f'⏱Total time: {total_time:.2f} s')
    print(f'API calls: {sum(api.query_count.values())}')
    for name, count in api.query_count.items():
        print(f'   {name}: {count}')
    
    print('\nStats generated successfully!')
    print(f'{config.user_name} | {config.linkedin_url}')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n\n Operation cancelled by user')
    except Exception as e:
        print(f'\nError: {str(e)}')
        raise