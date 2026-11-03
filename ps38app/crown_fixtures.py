"""Read current football identities using an already saved pool session.

Mirrors hdproc's get_game_list live/today flow. No login, session replacement,
odds persistence or order operations are performed here.
"""

import json
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from .account_pool.crown_member import _http_post, cookie_header

LIMIT = 8 * 1024 * 1024


class CrownFeedError(ValueError):
    """A deliberately sanitized message safe to display in the dashboard."""


def parse_games(body, *, live, offset_minutes=-240):
    if len(body) > LIMIT or b'<!DOCTYPE' in body.upper() or b'<!ENTITY' in body.upper():
        raise CrownFeedError('皇冠赛事响应格式无效，请稍后重新拉取')
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise CrownFeedError('皇冠返回的不是有效赛事列表，请检查会员会话') from None
    if root.tag != 'serverresponse':
        raise CrownFeedError('皇冠未返回有效赛事列表，请检查会员会话')
    if (root.findtext('code') or '').strip().lower() == 'error':
        if (root.findtext('msg') or '').strip().lower() == 'doublelogin':
            raise CrownFeedError('皇冠提示重复登录；请在号池重新登录会员后拉取赛事')
        raise CrownFeedError('皇冠会员会话已失效，请在号池重新登录')
    original = root.findtext('.//original')
    games = []
    if original:
        try:
            data = json.loads(original)
            games = list(data.values()) if isinstance(data, dict) else []
        except json.JSONDecodeError:
            pass  # Some feeds have broken original JSON but valid game XML.
    if not games:
        games = [{child.tag.upper(): child.text or '' for child in node} for node in root.iter('game')]
    if not games and root.find('dataCount') is None and original is None:
        raise CrownFeedError('皇冠未返回赛事列表，请稍后重新拉取')
    result = []
    for game in games:
        if not isinstance(game, dict) or not game.get('GID'):
            continue
        kickoff = str(game.get('GAME_DATE_TIME') or '').strip()
        try:
            stamp = datetime.fromisoformat(kickoff)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone(timedelta(minutes=offset_minutes)))
            kickoff = stamp.isoformat()
        except ValueError:
            kickoff = ''  # RETIMESET is a match clock, never a scheduled kickoff.
        result.append({
            'id': str(game['GID']), 'league_id': str(game.get('LID') or game.get('LEAGUE') or ''),
            'league': str(game.get('LEAGUE') or ''), 'home': str(game.get('TEAM_H') or ''),
            'away': str(game.get('TEAM_C') or ''), 'start_label': kickoff,
            'is_live': live, 'is_finished': str(game.get('IS_FINISHED') or '').lower() in {'1', 'true', 'y'},
            'matchStatus': str(game.get('MATCH_STATUS') or ''),
            'score': f"{game.get('SCORE_H')}:{game.get('SCORE_C')}" if game.get('SCORE_H') is not None and game.get('SCORE_C') is not None else '',
            'minute': str(game.get('RETIMESET') or '') if live else '',
        })
    return result


def fetch_pool_crown_events(config):
    db = Path(config.get('pool_db_path') or 'data/accounts.sqlite3')
    if not db.is_file():
        return [], '皇冠服务会话失效；请先在号池登录皇冠会员', ''
    try:
        with sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True) as connection:
            accounts = connection.execute(
                "SELECT id,base_url FROM accounts WHERE platform='crown_member' AND enabled=1 "
                "AND status IN ('online','error') ORDER BY (status='online') DESC,last_check DESC LIMIT 3"
            ).fetchall()
    except sqlite3.Error:
        accounts = []
    error = '皇冠当前没有可用会员会话，请先在号池登录皇冠会员'
    deadline = time.monotonic() + 12
    for account_id, base_url in accounts:
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', account_id):
            continue
        folder = Path(config.get('pool_dir') or 'data/accounts') / account_id
        try:
            meta = json.loads((folder / 'session_meta.json').read_text())
            state = json.loads((folder / 'profile/storage_state.json').read_text())
            origin = str(meta.get('origin') or base_url).rstrip('/')
            cookie = cookie_header(state, origin)
            if not cookie or not meta.get('uid') or not meta.get('ver'):
                error = '皇冠会员会话不完整或 Cookie 已过期，请在号池重新登录'
                continue
            headers = {'Cookie': cookie, 'Origin': origin, 'Referer': origin + '/',
                       'Content-Type': 'application/x-www-form-urlencoded',
                       'User-Agent': meta.get('user_agent') or 'Mozilla/5.0'}
            url = origin + '/transform.php?' + urlencode({'ver': meta['ver']})
            events = {}
            for mode, rtype in (('live', 'rb'), ('today', 'r')):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                form = {'p': 'get_game_list', 'uid': meta['uid'], 'ver': meta['ver'],
                        'langx': 'zh-cn', 'p3type': '', 'date': '', 'gtype': 'ft',
                        'showtype': mode, 'rtype': rtype, 'ltype': '4', 'filter': '',
                        'cupFantasy': 'N', 'sorttype': 'L', 'specialClick': '', 'isFantasy': 'N',
                        'ts': str(int(time.time() * 1000)), 'chgSortTS': str(int(time.time() * 1000))}
                for attempt in range(2):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    try:
                        status, body = _http_post(url, urlencode(form).encode(), headers, min(5, remaining),
                            ignore_https_errors=bool(config.get('pool_crown_ignore_https_errors', True)),
                            max_response_bytes=LIMIT)
                        break
                    except HTTPError:
                        raise  # Explicit session rejection is not a transient connection error.
                    except (URLError, OSError):
                        if attempt:
                            raise
                if status != 200:
                    raise CrownFeedError(f'皇冠赛事接口暂不可用（HTTP {status}）')
                for event in parse_games(body, live=mode == 'live',
                                         offset_minutes=int(config.get('crown_utc_offset_minutes', -240))):
                    events.setdefault(event['id'], event)  # Keep the live version.
            return list(events.values()), None, datetime.now().astimezone().isoformat(timespec='seconds')
        except CrownFeedError as exc:
            error = str(exc)
        except HTTPError as exc:
            error = ('皇冠会员会话已失效，请在号池重新登录' if exc.code in (401, 440)
                     else f'皇冠赛事接口暂不可用（HTTP {exc.code}），请稍后重新拉取')
        except (URLError, OSError, TimeoutError):
            error = '皇冠会员赛事连接失败或超时，会话已保留，请稍后重新拉取'
        except (ValueError, TypeError, KeyError):
            error = '皇冠会员会话或赛事格式不完整，请在号池检查后重新拉取'
    return [], error, ''
