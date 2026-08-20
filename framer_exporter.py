#!/usr/bin/env python3
"""
Framer Site Exporter - Полное копирование ресурсов сайта Framer
Копирует HTML, CSS, JavaScript, медиафайлы и все ассеты
"""

import os
import sys
import argparse
import hashlib
from urllib.parse import urljoin, urlparse, unquote
from pathlib import Path
import requests
from bs4 import BeautifulSoup
import re
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Set, Dict, List, Tuple, Optional
import logging

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class FramerSiteExporter:
    """Экспортёр сайтов Framer с полным копированием ресурсов"""
    
    # Расширения файлов для разных типов ресурсов
    MEDIA_EXTENSIONS = {
        'image': {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.ico', '.bmp', '.avif'},
        'video': {'.mp4', '.webm', '.ogg', '.mov', '.avi'},
        'audio': {'.mp3', '.wav', '.ogg', '.aac'},
        'font': {'.woff', '.woff2', '.ttf', '.eot', '.otf'},
        'document': {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx'}
    }
    
    # Домены CDN Framer
    FRAMER_CDN_DOMAINS = {
        'framer.com',
        'framerusercontent.com',
        'framer.media',
        'cdn.framer.com',
        'assets.framer.com'
    }
    
    def __init__(self, base_url: str, output_dir: str, max_workers: int = 10):
        """
        Инициализация экспортёра
        
        Args:
            base_url: URL сайта Framer для экспорта
            output_dir: Директория для сохранения экспортированных файлов
            max_workers: Максимальное количество потоков для загрузки
        """
        self.base_url = base_url.rstrip('/')
        self.output_dir = Path(output_dir)
        self.max_workers = max_workers
        
        # Множество посещённых URL для предотвращения дублирования
        self.visited_urls: Set[str] = set()
        
        # Карта соответствия URL -> локальный путь
        self.url_to_path: Dict[str, str] = {}
        
        # Сессия requests для переиспользования соединений
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': self.base_url,
        })
        
        # Статистика
        self.stats = {
            'html_pages': 0,
            'css_files': 0,
            'js_files': 0,
            'images': 0,
            'videos': 0,
            'audio': 0,
            'fonts': 0,
            'documents': 0,
            'other': 0,
            'errors': 0
        }
        
    def get_resource_type(self, url: str) -> str:
        """Определение типа ресурса по URL"""
        parsed = urlparse(url)
        path = parsed.path.lower()
        ext = Path(path).suffix.lower()
        
        for resource_type, extensions in self.MEDIA_EXTENSIONS.items():
            if ext in extensions:
                return resource_type
        
        if ext == '.css':
            return 'css'
        elif ext == '.js':
            return 'javascript'
        elif ext in {'.html', '.htm'} or path.endswith('/'):
            return 'html'
        
        return 'other'
    
    def get_local_path(self, url: str, resource_type: str) -> Path:
        """
        Генерация локального пути для URL
        
        Args:
            url: Исходный URL
            resource_type: Тип ресурса
            
        Returns:
            Локальный путь для сохранения файла
        """
        parsed = urlparse(url)
        
        # Создаём хэш от URL для уникальности
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        
        # Очищаем путь от query параметров
        path = unquote(parsed.path)
        
        # Определяем базовую директорию в зависимости от типа ресурса
        if resource_type == 'html':
            base_dir = self.output_dir
            if path and path != '/':
                # Сохраняем структуру путей для HTML
                filename = Path(path).name or 'index.html'
                if not filename.endswith('.html'):
                    filename += '.html'
                subpath = str(Path(path).parent).lstrip('/')
                if subpath == '.' or subpath == '':
                    return base_dir / filename
                return base_dir / subpath / filename
            else:
                return base_dir / 'index.html'
        
        elif resource_type == 'css':
            base_dir = self.output_dir / 'css'
        elif resource_type == 'javascript':
            base_dir = self.output_dir / 'js'
        elif resource_type == 'image':
            base_dir = self.output_dir / 'images'
        elif resource_type == 'video':
            base_dir = self.output_dir / 'videos'
        elif resource_type == 'audio':
            base_dir = self.output_dir / 'audio'
        elif resource_type == 'font':
            base_dir = self.output_dir / 'fonts'
        elif resource_type == 'document':
            base_dir = self.output_dir / 'documents'
        else:
            base_dir = self.output_dir / 'assets'
        
        # Извлекаем имя файла из пути или генерируем его
        if path and path != '/':
            filename = Path(path).name
            if not filename:
                filename = f'asset_{url_hash}'
        else:
            filename = f'asset_{url_hash}'
        
        # Добавляем расширение если нет
        if '.' not in filename:
            ext_map = {
                'css': '.css',
                'javascript': '.js',
                'image': '.jpg',
                'video': '.mp4',
                'audio': '.mp3',
                'font': '.woff2',
                'document': '.bin'
            }
            filename += ext_map.get(resource_type, '.bin')
        
        return base_dir / filename
    
    def download_file(self, url: str, save_path: Path) -> bool:
        """
        Загрузка файла по URL
        
        Args:
            url: URL для загрузки
            save_path: Путь для сохранения
            
        Returns:
            True если загрузка успешна, False иначе
        """
        try:
            # Создаём директорию если не существует
            save_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Проверяем есть ли уже файл
            if save_path.exists():
                logger.debug(f"Файл уже существует: {save_path}")
                return True
            
            response = self.session.get(url, stream=True, timeout=30)
            response.raise_for_status()
            
            # Записываем файл
            with open(save_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            logger.info(f"Загружено: {url} -> {save_path}")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка загрузки {url}: {e}")
            self.stats['errors'] += 1
            return False
    
    def extract_resources_from_html(self, html_content: str, base_url: str) -> Dict[str, List[str]]:
        """
        Извлечение всех ресурсов из HTML
        
        Args:
            html_content: HTML контент
            base_url: Базовый URL для разрешения относительных путей
            
        Returns:
            Словарь с типами ресурсов и их URL
        """
        resources = {
            'css': [],
            'javascript': [],
            'images': [],
            'videos': [],
            'audio': [],
            'fonts': [],
            'links': []
        }
        
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # CSS файлы (link rel="stylesheet")
        for link in soup.find_all('link', rel=lambda x: x and 'stylesheet' in x.lower()):
            href = link.get('href')
            if href:
                resources['css'].append(urljoin(base_url, href))
        
        # JavaScript файлы
        for script in soup.find_all('script', src=True):
            src = script.get('src')
            if src:
                resources['javascript'].append(urljoin(base_url, src))
        
        # Изображения
        for img in soup.find_all('img', src=True):
            src = img.get('src')
            if src:
                resources['images'].append(urljoin(base_url, src))
        
        # Изображения в srcset
        for img in soup.find_all('img', srcset=True):
            srcset = img.get('srcset')
            if srcset:
                for src in srcset.split(','):
                    url = src.strip().split()[0]
                    resources['images'].append(urljoin(base_url, url))
        
        # Picture source
        for source in soup.find_all('source', srcset=True):
            srcset = source.get('srcset')
            if srcset:
                for src in srcset.split(','):
                    url = src.strip().split()[0]
                    resources['images'].append(urljoin(base_url, url))
        
        # Видео
        for video in soup.find_all('video'):
            if video.get('src'):
                resources['videos'].append(urljoin(base_url, video.get('src')))
            for source in video.find_all('source'):
                if source.get('src'):
                    resources['videos'].append(urljoin(base_url, source.get('src')))
        
        # Аудио
        for audio in soup.find_all('audio'):
            if audio.get('src'):
                resources['audio'].append(urljoin(base_url, audio.get('src')))
            for source in audio.find_all('source'):
                if source.get('src'):
                    resources['audio'].append(urljoin(base_url, source.get('src')))
        
        # Фоновые изображения в style атрибутах
        for tag in soup.find_all(style=True):
            style = tag.get('style')
            if style:
                # Поиск url() в style
                urls = re.findall(r'url\(["\']?([^"\')]+)["\']?\)', style)
                for url in urls:
                    if not url.startswith('data:'):
                        resources['images'].append(urljoin(base_url, url))
        
        # CSS импорт в style тегах
        for style in soup.find_all('style'):
            if style.string:
                urls = re.findall(r'@import\s+["\']([^"\']+)["\']', style.string)
                for url in urls:
                    resources['css'].append(urljoin(base_url, url))
        
        # Ссылки на другие страницы
        for link in soup.find_all('a', href=True):
            href = link.get('href')
            if href and not href.startswith(('mailto:', 'tel:', 'javascript:', '#')):
                full_url = urljoin(base_url, href)
                # Проверяем что это тот же домен
                if urlparse(full_url).netloc == urlparse(self.base_url).netloc:
                    resources['links'].append(full_url)
        
        # Удаление дубликатов
        for key in resources:
            resources[key] = list(set(resources[key]))
        
        return resources
    
    def update_resource_paths_in_html(self, html_content: str, base_url: str) -> str:
        """
        Обновление путей к ресурсам в HTML для локального использования
        
        Args:
            html_content: Исходный HTML
            base_url: Базовый URL
            
        Returns:
            HTML с обновлёнными путями
        """
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Обновляем CSS ссылки
        for link in soup.find_all('link', rel=lambda x: x and 'stylesheet' in x.lower(), href=True):
            old_href = link.get('href')
            full_url = urljoin(base_url, old_href)
            if full_url in self.url_to_path:
                local_path = self.get_relative_path(self.url_to_path[full_url])
                link['href'] = local_path
        
        # Обновляем JavaScript ссылки
        for script in soup.find_all('script', src=True):
            old_src = script.get('src')
            full_url = urljoin(base_url, old_src)
            if full_url in self.url_to_path:
                local_path = self.get_relative_path(self.url_to_path[full_url])
                script['src'] = local_path
        
        # Обновляем изображения
        for img in soup.find_all('img', src=True):
            old_src = img.get('src')
            full_url = urljoin(base_url, old_src)
            if full_url in self.url_to_path:
                local_path = self.get_relative_path(self.url_to_path[full_url])
                img['src'] = local_path
        
        # Обновляем srcset
        for img in soup.find_all('img', srcset=True):
            old_srcset = img.get('srcset')
            new_srcset = []
            for src in old_srcset.split(','):
                parts = src.strip().split()
                url = parts[0]
                size = parts[1] if len(parts) > 1 else ''
                full_url = urljoin(base_url, url)
                if full_url in self.url_to_path:
                    local_path = self.get_relative_path(self.url_to_path[full_url])
                    new_srcset.append(f"{local_path} {size}".strip())
            if new_srcset:
                img['srcset'] = ', '.join(new_srcset)
        
        # Обновляем видео и аудио
        for tag_name in ['video', 'audio']:
            for tag in soup.find_all(tag_name):
                if tag.get('src'):
                    old_src = tag.get('src')
                    full_url = urljoin(base_url, old_src)
                    if full_url in self.url_to_path:
                        local_path = self.get_relative_path(self.url_to_path[full_url])
                        tag['src'] = local_path
                
                for source in tag.find_all('source'):
                    if source.get('src'):
                        old_src = source.get('src')
                        full_url = urljoin(base_url, old_src)
                        if full_url in self.url_to_path:
                            local_path = self.get_relative_path(self.url_to_path[full_url])
                            source['src'] = local_path
        
        # Обновляем ссылки
        for link in soup.find_all('a', href=True):
            old_href = link.get('href')
            if old_href and not old_href.startswith(('mailto:', 'tel:', 'javascript:', '#')):
                full_url = urljoin(base_url, old_href)
                if full_url in self.url_to_path:
                    local_path = self.get_relative_path(self.url_to_path[full_url])
                    link['href'] = local_path
        
        return str(soup)
    
    def get_relative_path(self, target_path: Path) -> str:
        """Получение относительного пути от текущего файла"""
        # Для простоты возвращаем абсолютный путь от корня export
        return target_path.relative_to(self.output_dir)
    
    def process_page(self, url: str) -> bool:
        """
        Обработка одной страницы
        
        Args:
            url: URL страницы
            
        Returns:
            True если обработка успешна
        """
        if url in self.visited_urls:
            return True
        
        self.visited_urls.add(url)
        logger.info(f"Обработка страницы: {url}")
        
        try:
            response = self.session.get(url, timeout=30)
            response.raise_for_status()
            
            # Определяем тип контента
            content_type = response.headers.get('Content-Type', '').lower()
            
            if 'text/html' not in content_type:
                logger.warning(f"URL {url} не является HTML страницей")
                return True
            
            html_content = response.text
            base_url = response.url  # Используем финальный URL после редиректов
            
            # Извлекаем ресурсы
            resources = self.extract_resources_from_html(html_content, base_url)
            
            # Загружаем ресурсы параллельно
            tasks = []
            
            # CSS
            for css_url in resources['css']:
                if css_url not in self.visited_urls:
                    resource_type = self.get_resource_type(css_url)
                    local_path = self.get_local_path(css_url, resource_type)
                    self.url_to_path[css_url] = local_path
                    tasks.append((css_url, local_path, 'css'))
            
            # JavaScript
            for js_url in resources['javascript']:
                if js_url not in self.visited_urls:
                    resource_type = self.get_resource_type(js_url)
                    local_path = self.get_local_path(js_url, resource_type)
                    self.url_to_path[js_url] = local_path
                    tasks.append((js_url, local_path, 'javascript'))
            
            # Изображения
            for img_url in resources['images']:
                if img_url not in self.visited_urls and not img_url.startswith('data:'):
                    resource_type = self.get_resource_type(img_url)
                    local_path = self.get_local_path(img_url, resource_type)
                    self.url_to_path[img_url] = local_path
                    tasks.append((img_url, local_path, 'image'))
            
            # Видео
            for video_url in resources['videos']:
                if video_url not in self.visited_urls:
                    resource_type = self.get_resource_type(video_url)
                    local_path = self.get_local_path(video_url, resource_type)
                    self.url_to_path[video_url] = local_path
                    tasks.append((video_url, local_path, 'video'))
            
            # Аудио
            for audio_url in resources['audio']:
                if audio_url not in self.visited_urls:
                    resource_type = self.get_resource_type(audio_url)
                    local_path = self.get_local_path(audio_url, resource_type)
                    self.url_to_path[audio_url] = local_path
                    tasks.append((audio_url, local_path, 'audio'))
            
            # Выполняем загрузку в потоках
            with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
                futures = {
                    executor.submit(self.download_file, url, path): (url, rtype)
                    for url, path, rtype in tasks
                }
                
                for future in as_completed(futures):
                    url, rtype = futures[future]
                    try:
                        if future.result():
                            # Корректное обновление статистики
                            if rtype == 'css':
                                self.stats['css_files'] += 1
                            elif rtype == 'javascript':
                                self.stats['js_files'] += 1
                            elif rtype == 'image':
                                self.stats['images'] += 1
                            elif rtype == 'video':
                                self.stats['videos'] += 1
                            elif rtype == 'audio':
                                self.stats['audio'] += 1
                            elif rtype == 'font':
                                self.stats['fonts'] += 1
                            elif rtype == 'document':
                                self.stats['documents'] += 1
                            else:
                                self.stats['other'] += 1
                    except Exception as e:
                        logger.error(f"Ошибка при загрузке {url}: {e}")
            
            # Обновляем пути в HTML и сохраняем
            updated_html = self.update_resource_paths_in_html(html_content, base_url)
            
            local_path = self.get_local_path(url, 'html')
            local_path.parent.mkdir(parents=True, exist_ok=True)
            
            with open(local_path, 'w', encoding='utf-8') as f:
                f.write(updated_html)
            
            self.stats['html_pages'] += 1
            self.url_to_path[url] = local_path
            
            # Рекурсивно обрабатываем найденные ссылки
            for link_url in resources['links']:
                if link_url not in self.visited_urls:
                    self.process_page(link_url)
            
            return True
            
        except Exception as e:
            logger.error(f"Ошибка обработки страницы {url}: {e}")
            self.stats['errors'] += 1
            return False
    
    def export(self) -> bool:
        """
        Запуск процесса экспорта
        
        Returns:
            True если экспорт успешен
        """
        logger.info(f"Начало экспорта сайта: {self.base_url}")
        logger.info(f"Директория назначения: {self.output_dir}")
        
        # Создаём выходную директорию
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Начинаем с главной страницы
        success = self.process_page(self.base_url)
        
        # Вывод статистики
        print("\n" + "="*50)
        print("СТАТИСТИКА ЭКСПОРТА")
        print("="*50)
        print(f"HTML страниц: {self.stats['html_pages']}")
        print(f"CSS файлов: {self.stats['css_files']}")
        print(f"JavaScript файлов: {self.stats['js_files']}")
        print(f"Изображений: {self.stats['images']}")
        print(f"Видео: {self.stats['videos']}")
        print(f"Аудио: {self.stats['audio']}")
        print(f"Шрифтов: {self.stats['fonts']}")
        print(f"Документов: {self.stats['documents']}")
        print(f"Других файлов: {self.stats['other']}")
        print(f"Ошибок: {self.stats['errors']}")
        print("="*50)
        
        if self.stats['errors'] > 0:
            logger.warning(f"Экспорт завершён с {self.stats['errors']} ошибками")
        
        return success and self.stats['errors'] == 0


def main():
    parser = argparse.ArgumentParser(
        description='Framer Site Exporter - Полное копирование ресурсов сайта Framer',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:
  python framer_exporter.py https://example.framer.website -o ./export
  python framer_exporter.py https://my-site.framer.app --output ./backup --workers 20
        """
    )
    
    parser.add_argument(
        'url',
        help='URL сайта Framer для экспорта'
    )
    
    parser.add_argument(
        '-o', '--output',
        default='./framer_export',
        help='Директория для сохранения экспортированных файлов (по умолчанию: ./framer_export)'
    )
    
    parser.add_argument(
        '-w', '--workers',
        type=int,
        default=10,
        help='Максимальное количество потоков для загрузки (по умолчанию: 10)'
    )
    
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Включить подробное логирование'
    )
    
    args = parser.parse_args()
    
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    
    try:
        exporter = FramerSiteExporter(
            base_url=args.url,
            output_dir=args.output,
            max_workers=args.workers
        )
        
        success = exporter.export()
        
        if success:
            print(f"\n✅ Экспорт успешно завершён!")
            print(f"📁 Файлы сохранены в: {Path(args.output).absolute()}")
            sys.exit(0)
        else:
            print(f"\n⚠️  Экспорт завершён с предупреждениями")
            sys.exit(1)
            
    except KeyboardInterrupt:
        print("\n❌ Экспорт прерван пользователем")
        sys.exit(130)
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
