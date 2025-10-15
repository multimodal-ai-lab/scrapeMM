import logging
import re
import ssl
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from ezmm import MultimodalSequence, download_image, download_video, Item

from scrapemm.secrets import get_secret
from scrapemm.integrations.base import RetrievalIntegration
from scrapemm.util import get_domain

logger = logging.getLogger("scrapeMM")


class Reddit(RetrievalIntegration):
    """The Reddit integration for retrieving posts, comments, and subreddit info.
    
    Uses Reddit API with OAuth2 authentication.
    """
    
    domains = ["reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com", "redd.it"]

    def __init__(self):
        self.client_id = get_secret("reddit_client_id")
        self.client_secret = get_secret("reddit_client_secret")
        self.username = get_secret("reddit_username")
        self.password = get_secret("reddit_password")
        self.user_agent = get_secret("reddit_user_agent") or "scrapeMM/1.0"
        
        self.access_token = None
        self.subreddit_cache = {}  # Cache subreddit descriptions to avoid redundant API calls
        
        # Check if we have the required credentials
        if self.client_id and self.client_secret:
            self.connected = True
            logger.info("✅ Reddit integration configured.")
        else:
            self.connected = False
            logger.warning("❌ Reddit integration not configured: Missing client credentials.")

    def _create_ssl_context(self):
        """Create SSL context that doesn't verify certificates (for development)."""
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        return ssl_context

    async def get(self, url: str, session: aiohttp.ClientSession) -> Optional[MultimodalSequence]:
        """Retrieves content from a Reddit URL."""
        if not self.connected:
            logger.error("❌ Reddit integration not connected.")
            return None
            
        if get_domain(url) not in self.domains:
            logger.error(f"❌ Invalid domain for Reddit: {get_domain(url)}")
            return None

        # Check if the provided session is usable, create a new one if not
        session_created = False
        try:
            if session.closed:
                connector = aiohttp.TCPConnector(ssl=self._create_ssl_context())
                session = aiohttp.ClientSession(connector=connector)
                session_created = True
        except Exception as e:
            # If session checking fails, create a new one
            logger.debug(f"Session check failed, creating new session: {e}")
            try:
                connector = aiohttp.TCPConnector(ssl=self._create_ssl_context())
                session = aiohttp.ClientSession(connector=connector)
                session_created = True
            except Exception as session_error:
                logger.error(f"❌ Failed to create Reddit session: {session_error}")
                return None

        try:
            # Authenticate if not already done
            if not self.access_token:
                await self._authenticate(session)
                if not self.access_token:
                    logger.error("❌ Failed to authenticate with Reddit API.")
                    return None

            # Determine content type from URL
            if self._is_post_url(url):
                result = await self._get_post(url, session)
            elif self._is_subreddit_url(url):
                result = await self._get_subreddit(url, session)
            elif self._is_user_url(url):
                result = await self._get_user(url, session)
            else:
                logger.error(f"❌ Unsupported Reddit URL format: {url}")
                result = None
                
            return result
        except Exception as e:
            if "Event loop is closed" in str(e):
                logger.warning(f"Event loop closed error for {url}: {e}")
                # Try once more with a fresh session in case of event loop issues
                try:
                    connector = aiohttp.TCPConnector(ssl=self._create_ssl_context())
                    fresh_session = aiohttp.ClientSession(connector=connector)
                    if not self.access_token:
                        await self._authenticate(fresh_session)
                    
                    if self._is_post_url(url):
                        result = await self._get_post(url, fresh_session)
                    elif self._is_subreddit_url(url):
                        result = await self._get_subreddit(url, fresh_session)
                    elif self._is_user_url(url):
                        result = await self._get_user(url, fresh_session)
                    else:
                        result = None
                    
                    await fresh_session.close()
                    return result
                except Exception as retry_error:
                    logger.error(f"❌ Retry failed for {url}: {retry_error}")
                    return None
            else:
                logger.error(f"❌ Error retrieving Reddit content for {url}: {e}")
                return None
        finally:
            # Close session if we created it
            if session_created:
                try:
                    if not session.closed:
                        await session.close()
                except Exception as close_error:
                    logger.debug(f"Error closing session: {close_error}")

    async def _authenticate(self, session: aiohttp.ClientSession) -> bool:
        """Authenticates with Reddit API using client credentials or user credentials."""
        try:
            auth_url = "https://www.reddit.com/api/v1/access_token"
            
            # Try user authentication first (if username/password provided)
            if self.username and self.password:
                auth_data = {
                    'grant_type': 'password',
                    'username': self.username,
                    'password': self.password
                }
                logger.info("Attempting Reddit authentication with user credentials...")
            else:
                # Fall back to client credentials flow
                auth_data = {
                    'grant_type': 'client_credentials'
                }
                logger.info("Attempting Reddit authentication with client credentials...")
            
            auth = aiohttp.BasicAuth(self.client_id, self.client_secret)
            headers = {
                'User-Agent': self.user_agent
            }
            
            async with session.post(auth_url, data=auth_data, auth=auth, headers=headers, ssl=self._create_ssl_context()) as response:
                if response.status == 200:
                    data = await response.json()
                    self.access_token = data.get('access_token')
                    logger.info("✅ Successfully authenticated with Reddit API.")
                    return True
                else:
                    error_text = await response.text()
                    logger.error(f"❌ Reddit authentication failed: {response.status} - {error_text}")
                    return False
                    
        except Exception as e:
            logger.error(f"❌ Error authenticating with Reddit: {e}")
            return False

    async def _get_post(self, url: str, session: aiohttp.ClientSession) -> Optional[MultimodalSequence]:
        """Retrieves a Reddit post."""
        # Extract post info first (needed for retry logic)
        post_info = self._extract_post_info(url)
        if not post_info:
            logger.error(f"❌ Could not extract post info from URL: {url}")
            return None
            
        subreddit, post_id = post_info
        logger.debug(f"Extracted subreddit: {subreddit}, post_id: {post_id} from URL: {url}")
        
        try:
            
            # Use Reddit API to get post data
            api_url = f"https://oauth.reddit.com/r/{subreddit}/comments/{post_id}.json"
            headers = {
                'Authorization': f'Bearer {self.access_token}',
                'User-Agent': self.user_agent
            }
            
            logger.debug(f"Making Reddit API request to: {api_url}")
            
            async with session.get(api_url, headers=headers, ssl=self._create_ssl_context()) as response:
                if response.status != 200:
                    logger.error(f"❌ Reddit API error: {response.status} for URL: {api_url}")
                    error_text = await response.text()
                    logger.error(f"Error response: {error_text}")
                    return None
                    
                data = await response.json()
                
                if not data or len(data) < 1:
                    logger.error("❌ No post data received from Reddit API")
                    return None
                
                post_data = data[0]['data']['children'][0]['data']
                comments_data = data[1]['data']['children'] if len(data) > 1 else []
                
                return await self._create_post_sequence(post_data, comments_data, url, session)
                
        except Exception as e:
            if "Event loop is closed" in str(e):
                logger.warning(f"❌ Event loop closed during Reddit API request for {url}: {e}")
                # Try once more with a fresh session in case of event loop issues
                try:
                    connector = aiohttp.TCPConnector(ssl=self._create_ssl_context())
                    fresh_session = aiohttp.ClientSession(connector=connector)
                    
                    # Re-authenticate with fresh session if needed
                    if not self.access_token:
                        await self._authenticate(fresh_session)
                    
                    # Retry the API request
                    api_url = f"https://oauth.reddit.com/r/{subreddit}/comments/{post_id}.json"
                    headers = {
                        'Authorization': f'Bearer {self.access_token}',
                        'User-Agent': self.user_agent
                    }
                    
                    logger.debug(f"Retrying Reddit API request to: {api_url}")
                    
                    async with fresh_session.get(api_url, headers=headers, ssl=self._create_ssl_context()) as response:
                        if response.status != 200:
                            logger.error(f"❌ Reddit API retry failed: {response.status} for URL: {api_url}")
                            await fresh_session.close()
                            return None
                            
                        data = await response.json()
                        
                        if not data or len(data) < 1:
                            logger.error("❌ No post data received from Reddit API on retry")
                            await fresh_session.close()
                            return None
                        
                        post_data = data[0]['data']['children'][0]['data']
                        comments_data = data[1]['data']['children'] if len(data) > 1 else []
                        
                        result = await self._create_post_sequence(post_data, comments_data, url, fresh_session)
                        await fresh_session.close()
                        logger.info(f"✅ Reddit post retry successful for {url}")
                        return result
                        
                except Exception as retry_error:
                    logger.error(f"❌ Reddit post retry failed for {url}: {retry_error}")
                    return None
            else:
                logger.error(f"❌ Error retrieving Reddit post: {e}")
            return None

    async def _get_subreddit(self, url: str, session: aiohttp.ClientSession) -> Optional[MultimodalSequence]:
        """Retrieves subreddit information."""
        try:
            subreddit_name = self._extract_subreddit_name(url)
            if not subreddit_name:
                return None
            
            api_url = f"https://oauth.reddit.com/r/{subreddit_name}/about"
            headers = {
                'Authorization': f'Bearer {self.access_token}',
                'User-Agent': self.user_agent
            }
            
            async with session.get(api_url, headers=headers, ssl=self._create_ssl_context()) as response:
                if response.status != 200:
                    logger.error(f"❌ Reddit API error for subreddit: {response.status}")
                    return None
                    
                data = await response.json()
                subreddit_data = data.get('data', {})
                
                return await self._create_subreddit_sequence(subreddit_data, url, session)
                
        except Exception as e:
            logger.error(f"❌ Error retrieving Reddit subreddit: {e}")
            return None

    async def _get_user(self, url: str, session: aiohttp.ClientSession) -> Optional[MultimodalSequence]:
        """Retrieves Reddit user information."""
        try:
            username = self._extract_username(url)
            if not username:
                return None
            
            api_url = f"https://oauth.reddit.com/user/{username}/about"
            headers = {
                'Authorization': f'Bearer {self.access_token}',
                'User-Agent': self.user_agent
            }
            
            async with session.get(api_url, headers=headers, ssl=self._create_ssl_context()) as response:
                if response.status != 200:
                    logger.error(f"❌ Reddit API error for user: {response.status}")
                    return None
                    
                data = await response.json()
                user_data = data.get('data', {})
                
                return await self._create_user_sequence(user_data, url, session)
                
        except Exception as e:
            logger.error(f"❌ Error retrieving Reddit user: {e}")
            return None

    async def _get_subreddit_description(self, subreddit_name: str, session: aiohttp.ClientSession) -> Optional[str]:
        """Fetch subreddit description. Uses cache to avoid redundant API calls."""
        # Remove r/ prefix if present
        if subreddit_name.startswith('r/'):
            subreddit_name = subreddit_name[2:]
        
        # Check cache first
        if subreddit_name in self.subreddit_cache:
            return self.subreddit_cache[subreddit_name]
        
        try:
            api_url = f"https://oauth.reddit.com/r/{subreddit_name}/about"
            headers = {
                'Authorization': f'Bearer {self.access_token}',
                'User-Agent': self.user_agent
            }
            
            async with session.get(api_url, headers=headers, ssl=self._create_ssl_context()) as response:
                if response.status != 200:
                    logger.warning(f"❌ Could not fetch subreddit description for {subreddit_name}: {response.status}")
                    self.subreddit_cache[subreddit_name] = None
                    return None
                    
                data = await response.json()
                subreddit_data = data.get('data', {})
                description = subreddit_data.get('public_description', '')
                
                # Cache the result
                self.subreddit_cache[subreddit_name] = description
                logger.info(f"✅ Fetched subreddit description for r/{subreddit_name}: {description[:50]}...")
                return description
                
        except Exception as e:
            logger.warning(f"❌ Error fetching subreddit description for {subreddit_name}: {e}")
            self.subreddit_cache[subreddit_name] = None
            return None

    async def _create_post_sequence(self, post_data: dict, comments_data: list, url: str, session: aiohttp.ClientSession) -> MultimodalSequence:
        """Creates MultimodalSequence from Reddit post data."""
        title = post_data.get('title', '')
        text = post_data.get('selftext', '')
        author = post_data.get('author', 'Unknown')
        subreddit = post_data.get('subreddit_name_prefixed', '')
        upvotes = post_data.get('ups', 0)
        downvotes = post_data.get('downs', 0)
        score = post_data.get('score', 0)
        upvote_ratio = post_data.get('upvote_ratio', 0)
        num_comments = post_data.get('num_comments', 0)
        
        # Additional fact-checking relevant fields
        is_original_content = post_data.get('is_original_content', False)
        domain = post_data.get('domain', '')
        url_linked = post_data.get('url', '')
        is_self = post_data.get('is_self', False)
        is_reddit_media_domain = post_data.get('is_reddit_media_domain', False)
        over_18 = post_data.get('over_18', False)
        locked = post_data.get('locked', False)
        archived = post_data.get('archived', False)
        
        # Post flair and awards (credibility indicators)
        link_flair_text = post_data.get('link_flair_text', '')
        link_flair_type = post_data.get('link_flair_type', '')
        total_awards_received = post_data.get('total_awards_received', 0)
        
        timestamp = post_data.get('created_utc')
        if timestamp:
            timestamp = datetime.utcfromtimestamp(timestamp).strftime("%B %d, %Y at %H:%M UTC")

        media: list[Item] = []
        
        # Handle different media types
        if post_data.get('is_video'):
            video_url = post_data['media']['reddit_video']['fallback_url']
            video = await download_video(video_url, session)
            if video:
                media.append(video)
        elif post_data.get('post_hint') == 'image':
            image_url = post_data.get('url_overridden_by_dest')
            image = await download_image(image_url, session)
            if image:
                media.append(image)
        # Handle external v.redd.it video links
        elif url_linked and 'v.redd.it' in url_linked:
            logger.debug(f"Attempting to download video from v.redd.it link: {url_linked}")
            # Try to download the video from the external v.redd.it link
            # v.redd.it URLs typically have a DASH format, try common video formats
            video_formats = [
                f"{url_linked}/DASH_720.mp4?source=fallback",
                f"{url_linked}/DASH_480.mp4?source=fallback",
                f"{url_linked}/DASH_360.mp4?source=fallback",
                f"{url_linked}/HLS_AUDIO_64_AAC.aac",  # Sometimes audio only
                f"{url_linked}/DASH_AUDIO_128.mp4"     # Audio in MP4
            ]
            
            for i, video_url in enumerate(video_formats):
                try:
                    logger.debug(f"Trying video format {i+1}: {video_url}")
                    video = await download_video(video_url, session)
                    if video:
                        logger.info(f"✅ Successfully downloaded video from {video_url}")
                        media.append(video)
                        break  # Stop trying other formats once we get one
                except Exception as e:
                    logger.debug(f"Failed format {i+1} ({video_url}): {e}")
                    continue  # Try next format
            
            if not media:
                logger.warning(f"Failed to download video from any format for {url_linked}")
        
        # TODO: Handle galleries, external links, etc. Ask Mark about this
        # Note: Comments are stored in metadata for stance analysis but not displayed in text

        # Fetch subreddit description for context
        subreddit_description = await self._get_subreddit_description(subreddit, session)
        subreddit_context_line = ""
        if subreddit_description:
            subreddit_context_line = f"\n**Subreddit description**: {subreddit_description}"

        # External link analysis (still useful raw data)
        external_link_text = ""
        if not is_self and url_linked and url_linked != url:
            external_link_text = f"\n\n**External Link**: {url_linked}"
            if domain:
                external_link_text += f" (Domain: {domain})"

        full_text = f"""**Reddit Post by user u/{author}**
Subreddit: {subreddit}{subreddit_context_line}

Post author: u/{author}
Posted: {timestamp}
URL: {url}
Engagement: {upvotes:,} upvotes, {score:,} score ({upvote_ratio:.1%} upvote ratio)
Comments: {num_comments:,}{external_link_text}

**{title}**

{text}"""

        result = MultimodalSequence([full_text, *media])
        result.metadata = {
            "post_id": post_data.get('id'),
            "post_title": title,
            "post_text": text,
            "author": author,
            "subreddit": subreddit,
            "timestamp": post_data.get('created_utc'),
            "upvotes": upvotes,
            "downvotes": downvotes,
            "score": score,
            "upvote_ratio": upvote_ratio,
            "num_comments": num_comments,
            "is_original_content": is_original_content,
            "domain": domain,
            "url_linked": url_linked,
            "is_self": is_self,
            "is_reddit_media_domain": is_reddit_media_domain,
            "over_18": over_18,
            "locked": locked,
            "archived": archived,
            "link_flair_text": link_flair_text,
            "link_flair_type": link_flair_type,
            "total_awards_received": total_awards_received,
            "comments": [
                {
                    "id": f"comment_{i}",
                    "author": comment['data'].get('author', 'unknown'),
                    "body": comment['data']['body'],
                    "score": comment['data'].get('score', 0),
                    "awards": comment['data'].get('total_awards_received', 0),
                    "author_info": f"Comment author: u/{comment['data'].get('author', 'unknown')} ({comment['data'].get('score', 0)} points, {comment['data'].get('total_awards_received', 0)} awards)"
                } for i, comment in enumerate(comments_data[:5]) if 'body' in comment.get('data', {})
            ]
        }
        return result

    async def _create_subreddit_sequence(self, subreddit_data: dict, url: str, session: aiohttp.ClientSession) -> MultimodalSequence:
        name = subreddit_data.get('display_name_prefixed')
        description = subreddit_data.get('public_description')
        subscribers = subreddit_data.get('subscribers', 0)
        
        # Additional credibility indicators for subreddits
        created_utc = subreddit_data.get('created_utc')
        active_user_count = subreddit_data.get('active_user_count', 0)
        accounts_active = subreddit_data.get('accounts_active', 0)
        over_18 = subreddit_data.get('over_18', False)
        quarantine = subreddit_data.get('quarantine', False)
        subreddit_type = subreddit_data.get('subreddit_type', 'public')
        
        # Community rules and moderation
        submission_type = subreddit_data.get('submission_type', 'any')
        wiki_enabled = subreddit_data.get('wiki_enabled', False)
        
        age_text = ""
        if created_utc:
            creation_date = datetime.fromtimestamp(created_utc, tz=timezone.utc)
            age_years = (datetime.now(timezone.utc) - creation_date).days // 365
            age_text = f"Created: {creation_date.strftime('%B %Y')} ({age_years} years ago)"
        
        # Community information
        community_info = []
        if active_user_count > 0:
            community_info.append(f"{active_user_count:,} currently active")
        if wiki_enabled:
            community_info.append("Wiki enabled")
        if subreddit_type != 'public':
            community_info.append(f"{subreddit_type.title()} community")
        if over_18:
            community_info.append("NSFW content")
        if quarantine:
            community_info.append("Quarantined community")
        
        community_text = ""
        if community_info:
            community_text = f"\n**Community Info**: {', '.join(community_info)}"

        icon_url = subreddit_data.get('icon_img')
        banner_url = subreddit_data.get('banner_background_image')
        
        media = []
        if icon_url:
            icon = await download_image(icon_url, session)
            if icon:
                media.append(icon)
        if banner_url:
            banner = await download_image(banner_url.split('?')[0], session)
            if banner:
                media.append(banner)
        
        text = f"""**Reddit Subreddit: {name}**
Subscribers: {subscribers:,}
{age_text}{community_text}

{description}"""
        
        result = MultimodalSequence([text] + media)
        result.metadata = {
            "subreddit_name": name,
            "display_name": subreddit_data.get('display_name'),
            "description": description,
            "subscribers": subscribers,
            "created_utc": created_utc,
            "active_user_count": active_user_count,
            "accounts_active": accounts_active,
            "over_18": over_18,
            "quarantine": quarantine,
            "subreddit_type": subreddit_type,
            "submission_type": submission_type,
            "wiki_enabled": wiki_enabled,
            "community_info": community_info
        }
        return result

    async def _create_user_sequence(self, user_data: dict, url: str, session: aiohttp.ClientSession) -> MultimodalSequence:
        name = user_data.get('name')
        karma = user_data.get('total_karma', 0)
        link_karma = user_data.get('link_karma', 0)
        comment_karma = user_data.get('comment_karma', 0)
        
        # Account credibility indicators
        is_employee = user_data.get('is_employee', False)
        is_mod = user_data.get('is_mod', False)
        is_gold = user_data.get('is_gold', False)
        verified = user_data.get('verified', False)
        has_verified_email = user_data.get('has_verified_email', False)
        
        timestamp = user_data.get('created_utc')
        account_age_text = ""
        if timestamp:
            creation_date = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            account_age_days = (datetime.now(timezone.utc) - creation_date).days
            account_age_years = account_age_days // 365
            account_age_text = f"Account age: {account_age_years} years ({account_age_days:,} days)"
            timestamp = creation_date.strftime("%B %d, %Y")

        # Account status information
        account_status = []
        if is_employee:
            account_status.append("Reddit Employee")
        if is_mod:
            account_status.append("Moderator")
        if is_gold:
            account_status.append("Reddit Premium")
        if verified:
            account_status.append("Verified")
        if has_verified_email:
            account_status.append("Email verified")
        
        status_text = ""
        if account_status:
            status_text = f"\n**Account Status**: {', '.join(account_status)}"

        icon_url = user_data.get('icon_img')
        
        media = []
        if icon_url:
            icon = await download_image(icon_url.split('?')[0], session)
            if icon:
                media.append(icon)
        
        text = f"""**Reddit User: u/{name}**
Total Karma: {karma:,} (Link: {link_karma:,}, Comment: {comment_karma:,})
Account created: {timestamp}
{account_age_text}{status_text}
"""
        
        result = MultimodalSequence([text] + media)
        result.metadata = {
            "username": name,
            "total_karma": karma,
            "link_karma": link_karma,
            "comment_karma": comment_karma,
            "created_utc": user_data.get('created_utc'),
            "account_age_days": account_age_days if timestamp else None,
            "account_age_years": account_age_years if timestamp else None,
            "is_employee": is_employee,
            "is_mod": is_mod,
            "is_gold": is_gold,
            "verified": verified,
            "has_verified_email": has_verified_email,
            "account_status": account_status
        }
        return result

    def _is_post_url(self, url: str) -> bool:
        return bool(re.search(r"/r/.*/comments/", url))

    def _is_subreddit_url(self, url: str) -> bool:
        return bool(re.search(r"/r/", url)) and not self._is_post_url(url)

    def _is_user_url(self, url: str) -> bool:
        return bool(re.search(r"/user/", url))

    def _extract_post_info(self, url: str) -> tuple[str, str] | None:
        match = re.search(r"/r/(?P<subreddit>[^/]+)/comments/(?P<post_id>[^/]+)", url)
        if match:
            return match.group('subreddit'), match.group('post_id')
        return None

    def _extract_subreddit_name(self, url: str) -> str | None:
        match = re.search(r"/r/(?P<subreddit>[^/]+)", url)
        if match:
            return match.group('subreddit')
        return None

    def _extract_username(self, url: str) -> str | None:
        match = re.search(r"/user/(?P<username>[^/]+)", url)
        if match:
            return match.group('username')
        return None

    async def search(self, query: str, session: aiohttp.ClientSession, max_results: int = 10, start_date: str = None, end_date: str = None) -> list[str]:
        """Search Reddit for posts related to the query.
        
        Args:
            query: The search query text
            session: aiohttp session to use for requests
            max_results: Maximum number of results to return
            start_date: Optional start date for filtering results (YYYY-MM-DD format)
            end_date: Optional end date for filtering results (YYYY-MM-DD format)
            
        Returns:
            List of Reddit post URLs found for the query
        """
        if not self.connected:
            logger.warning("Reddit integration not connected, cannot search")
            return []
            
        # Authenticate if we haven't already
        if not self.access_token:
            auth_success = await self._authenticate(session)
            if not auth_success:
                logger.error("Failed to authenticate with Reddit for search")
                return []
        
        try:
            # Search Reddit using their search API
            search_url = "https://oauth.reddit.com/search"
            headers = {
                'Authorization': f'Bearer {self.access_token}',
                'User-Agent': self.user_agent
            }
            
            params = {
                'q': query,
                'type': 'link',  # Search for posts/links
                'limit': min(max_results, 100),  # Reddit API limit
                'sort': 'relevance',
                't': 'week'  # Time filter: hour, day, week, month, year, all
            }
            
            date_info = ""
            if start_date or end_date:
                date_info = f" (dates: {start_date} to {end_date})"
            logger.info(f"Searching Reddit for: '{query}'{date_info} (max {max_results} results)")
            
            # Convert date strings to timestamps for filtering
            start_timestamp = None
            end_timestamp = None
            if start_date:
                from datetime import datetime
                start_timestamp = datetime.strptime(start_date, "%Y-%m-%d").timestamp()
            if end_date:
                from datetime import datetime
                end_timestamp = datetime.strptime(end_date + " 23:59:59", "%Y-%m-%d %H:%M:%S").timestamp()
            
            async with session.get(search_url, headers=headers, params=params, ssl=self._create_ssl_context()) as response:
                if response.status == 200:
                    data = await response.json()
                    
                    urls = []
                    posts = data.get('data', {}).get('children', [])
                    
                    for post in posts:
                        post_data = post.get('data', {})
                        permalink = post_data.get('permalink')
                        created_utc = post_data.get('created_utc')
                        
                        # Apply date filtering if specified
                        if start_timestamp and created_utc and created_utc < start_timestamp:
                            continue
                        if end_timestamp and created_utc and created_utc > end_timestamp:
                            continue
                        
                        if permalink:
                            # Convert Reddit permalink to full URL
                            full_url = f"https://www.reddit.com{permalink}"
                            urls.append(full_url)
                            
                            if len(urls) >= max_results:
                                break
                    
                    logger.info(f"Found {len(urls)} Reddit posts for query: '{query}'")
                    return urls
                    
                elif response.status == 401:
                    # Token expired, try to re-authenticate
                    logger.info("Reddit access token expired, re-authenticating...")
                    self.access_token = None
                    auth_success = await self._authenticate(session)
                    if auth_success:
                        # Retry the search once with new token
                        return await self.search(query, session, max_results)
                    else:
                        logger.error("Failed to re-authenticate with Reddit")
                        return []
                else:
                    error_text = await response.text()
                    logger.error(f"Reddit search failed: {response.status} - {error_text}")
                    return []
                    
        except Exception as e:
            logger.error(f"Error searching Reddit: {e}")
            return []
