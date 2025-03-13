import requests
import json
import time
import pandas as pd
import os
import logging
from datetime import datetime
from tqdm import tqdm
import warnings
import threading
import random
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed

os.makedirs('logs', exist_ok=True)
log_file = f'logs/parser_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
logging.basicConfig(
    filename=log_file,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    encoding='utf-8'
)
file_handler = logging.FileHandler(log_file, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
logger = logging.getLogger('belgiss_parser')
logger.addHandler(file_handler)
logger.info("Логирование инициализировано")

class DummyLogger:
    def info(self, msg): pass
    def error(self, msg): print(f"ОШИБКА: {msg}")
    def warning(self, msg): pass
    def debug(self, msg): pass

warnings.filterwarnings('ignore', message='Unverified HTTPS request')

class BelgissParser:
    def __init__(self):
        self.base_url = "https://tsouz.belgiss.by"
        self.api_url = "https://api.belgiss.by/tsouz"
        self.verify_ssl = False
        self.max_threads = 15
        self.current_threads = min(6, max(4, self.max_threads // 8))
        self.max_retries = 10
        self.current_retries = self.max_retries
        self.retry_delay = 2
        self.delay_range = (0.1, 1.0)
        self.error_counter = 0
        self.success_counter = 0
        self.error_threshold = 3
        self.success_threshold = 10
        self.consecutive_fails = 0
        self.consecutive_success = 0
        self.adaptive_delay = self.delay_range[0]
        self.request_semaphore = threading.Semaphore(10)
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 YaBrowser/25.2.0.0 Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "ru",
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Connection": "keep-alive",
            "Origin": "https://tsouz.belgiss.by",
            "Referer": "https://tsouz.belgiss.by/",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
            "X-Requested-With": "XMLHttpRequest"
        }
        self.lock = threading.Lock()
        self.output_folder = os.path.abspath('.')
        self.session = requests.Session()
        adapter = HTTPAdapter(
            max_retries=Retry(
                total=self.max_retries,
                backoff_factor=0.3,
                status_forcelist=[500, 502, 503, 504, 429],
                allowed_methods=["GET", "POST"]
            ),
            pool_connections=max(50, self.max_threads),
            pool_maxsize=max(50, self.max_threads)
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        logger.info("Инициализация парсера успешно завершена")

    def make_request(self, method, url, **kwargs):
        with self.request_semaphore:
            if "headers" not in kwargs:
                kwargs["headers"] = self.headers
            kwargs["verify"] = self.verify_ssl
            timeout = kwargs.pop('timeout', 30)
            for attempt in range(self.current_retries):
                try:
                    delay = random.uniform(self.delay_range[0], self.delay_range[1])
                    time.sleep(delay)
                    response = self.session.request(method, url, timeout=timeout, **kwargs)
                    status_code = response.status_code
                    if status_code == 429:
                        wait_time = min(self.retry_delay * (1.2 ** attempt) + random.uniform(0.5, 1), 8)
                        logger.info(f"Лимит запросов (429), ожидание {wait_time:.1f} сек...")
                        time.sleep(wait_time)
                        self.adjust_threads_and_retries(success=False)
                        continue
                    response.raise_for_status()
                    if response.text.strip():
                        try:
                            result = response.json()
                            self.adjust_threads_and_retries(success=True)
                            return result
                        except json.JSONDecodeError:
                            logger.error(f"Ошибка декодирования JSON из ответа для {url}")
                            self.adjust_threads_and_retries(success=False)
                            if attempt < self.current_retries - 1:
                                sleep_time = min(self.retry_delay * (attempt + 0.3) + random.uniform(0.3, 0.8), 5)
                                time.sleep(sleep_time)
                            else:
                                return None
                    else:
                        logger.error("Получен пустой ответ от сервера")
                        self.adjust_threads_and_retries(success=False)
                        if attempt < self.current_retries - 1:
                            sleep_time = min(self.retry_delay * (attempt + 0.3) + random.uniform(0.3, 0.8), 5)
                            time.sleep(sleep_time)
                        else:
                            return None
                except requests.exceptions.HTTPError as e:
                    logger.error(f"HTTP ошибка: {e}")
                    self.adjust_threads_and_retries(success=False)
                    if attempt < self.current_retries - 1:
                        sleep_time = min(self.retry_delay * (attempt + 0.3) + random.uniform(0.3, 0.8), 5)
                        logger.info(f"Повторная попытка через {sleep_time:.1f} сек...")
                        time.sleep(sleep_time)
                    else:
                        return None
                except requests.exceptions.ReadTimeout:
                    logger.error(f"Таймаут чтения для {url}")
                    self.adjust_threads_and_retries(success=False)
                    if attempt < self.current_retries - 1:
                        sleep_time = min(self.retry_delay * (attempt + 0.5) + random.uniform(0.5, 1), 6)
                        logger.info(f"Повторная попытка через {sleep_time:.1f} сек...")
                        time.sleep(sleep_time)
                    else:
                        return None
                except requests.exceptions.ConnectionError:
                    logger.error(f"Ошибка соединения для {url}")
                    self.adjust_threads_and_retries(success=False)
                    if attempt < self.current_retries - 1:
                        sleep_time = min(self.retry_delay * (attempt + 0.5) + random.uniform(0.5, 1), 6)
                        logger.info(f"Повторная попытка через {sleep_time:.1f} сек...")
                        time.sleep(sleep_time)
                    else:
                        return None
                except Exception as e:
                    logger.error(f"Непредвиденная ошибка при запросе {url}: {e}")
                    self.adjust_threads_and_retries(success=False)
                    if attempt < self.current_retries - 1:
                        sleep_time = min(self.retry_delay * (attempt + 0.3) + random.uniform(0.3, 0.7), 5)
                        time.sleep(sleep_time)
                    else:
                        return None
            return None

    def format_date(self, date_obj):
        return date_obj.strftime("%d.%m.%Y")

    def get_certifications_by_date_range(self, start_date, end_date, page=1, per_page=100):
        start_date_str = self.format_date(start_date)
        end_date_str = self.format_date(end_date)
        url = f"{self.api_url}/tsouz-certifs-light"
        params = {
            "page": page,
            "per-page": per_page,
            "sort": "-certdecltr_id",
            "filter[DocStartDate][gte]": start_date_str,
            "filter[DocStartDate][lte]": end_date_str,
            "query[trts]": 1
        }
        
        logger.info(f"Запрос сертификатов для дат {start_date_str} - {end_date_str}, страница {page}")
        
        response = self.make_request("GET", url, params=params)
        if response and isinstance(response, dict):
            items = response.get('items', [])
            meta = response.get('_meta', {})
            
            # Проверим наличие ключей и выведем более подробную информацию
            if not items:
                logger.warning(f"Получен пустой список сертификатов для дат {start_date_str} - {end_date_str}, страница {page}")
                logger.debug(f"Ответ API: {response}")
            else:
                logger.info(f"Получено {len(items)} сертификатов для дат {start_date_str} - {end_date_str}, страница {page}")
                
                # Логируем первые несколько ID для отладки
                if len(items) > 0:
                    sample_ids = [item.get('certdecltr_id', 'Н/Д') for item in items[:min(5, len(items))]]
                    logger.info(f"Примеры ID: {', '.join(map(str, sample_ids))}")
            
            result = {
                'items': items,
                'pages': meta.get('pageCount', 0),
                'count': meta.get('totalCount', 0),
                'current_page': meta.get('currentPage', page)
            }
            
            logger.info(f"Всего страниц: {result['pages']}, всего записей: {result['count']}")
            
            return result
        
        logger.error(f"Не удалось получить данные по сертификатам для дат {start_date_str} - {end_date_str}, страница {page}")
        return None

    def get_certification_details(self, doc_id):
        if not doc_id:
            logger.warning("Передан пустой ID сертификата")
            return None
        url = f"{self.api_url}/tsouz-certifs/{doc_id}"
        response = self.make_request("GET", url)
        
        # Добавляем отладочное логирование
        if response:
            cert_details = response.get('certdecltr_ConformityDocDetails', {})
            applicant = cert_details.get('ApplicantDetails', {})
            manufacturer_list = cert_details.get('ManufacturerDetails', [])
            
            logger.info(f"ID: {doc_id} - Получены данные сертификата")
            
            # Проверяем наличие данных заявителя
            if applicant:
                logger.info(f"ID: {doc_id} - Данные заявителя: Адрес={bool(applicant.get('Address'))}, Контакты={bool(applicant.get('ContactInfo'))}")
            else:
                logger.warning(f"ID: {doc_id} - Данные заявителя отсутствуют")
            
            # Проверяем наличие данных изготовителя
            if manufacturer_list and len(manufacturer_list) > 0:
                manufacturer = manufacturer_list[0]
                logger.info(f"ID: {doc_id} - Данные изготовителя: Адрес={bool(manufacturer.get('Address'))}, Контакты={bool(manufacturer.get('ContactInfo'))}")
            else:
                logger.warning(f"ID: {doc_id} - Данные изготовителя отсутствуют")
            
            # Проверяем наличие данных о продукте
            product_details = cert_details.get('ProductDetails', [])
            if product_details and len(product_details) > 0:
                logger.info(f"ID: {doc_id} - Данные о продукте: Имя={bool(product_details[0].get('ProductName'))}, Коды ТН ВЭД={bool(product_details[0].get('CommodityCodeList'))}")
            else:
                logger.warning(f"ID: {doc_id} - Данные о продукте отсутствуют")
        
        return response

    def fetch_page_worker(self, page, start_date, end_date, per_page, result_dict, pbar):
        page_data = self.get_certifications_by_date_range(start_date, end_date, page=page, per_page=per_page)
        if page_data and page_data.get('items'):
            with self.lock:
                result_dict[page] = [item.get('certdecltr_id') for item in page_data.get('items', [])]
                percent = int((pbar.n + 1) / pbar.total * 100)
                pbar.set_description(f"[1/2] Сбор списка | Потоки: {self.current_threads} | Стр: {page}")
                logger.info(f"Получено {len(result_dict[page])} сертификатов со страницы {page}")
        else:
            with self.lock:
                percent = int((pbar.n + 1) / pbar.total * 100)
                pbar.set_description(f"[1/2] Сбор списка | Потоки: {self.current_threads} | Стр: {page} (пусто)")
                logger.warning(f"Не удалось получить данные для страницы {page}")
        pbar.update(1)
        time.sleep(max(0.01, self.adaptive_delay / 4))

    def fetch_cert_details_worker(self, cert_id, result_list, pbar):
        """
        Рабочий метод для получения и обработки деталей сертификата
        """
        # Увеличиваем число попыток для важных запросов
        max_retries = self.current_retries + 1
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                cert_details = self.get_certification_details(cert_id)
                if cert_details:
                    processed_data = self.process_cert_data(cert_details)
                    
                    if processed_data:
                        with self.lock:
                            # Проверяем, не был ли этот сертификат уже добавлен
                            exists = False
                            for existing in result_list:
                                if existing.get('Регистрационный номер') == processed_data.get('Регистрационный номер'):
                                    exists = True
                                    break
                                    
                            if not exists:
                                result_list.append(processed_data)
                                pbar.set_description(f"[2/2] Детали | Потоки: {self.current_threads} | ID: {cert_id}")
                            else:
                                logger.info(f"Сертификат {cert_id} уже добавлен, пропускаем дубликат")
                                
                            pbar.update(1)
                            # Успешное получение, возвращаемся из функции
                            return
                    else:
                        with self.lock:
                            logger.warning(f"Не удалось обработать данные для сертификата {cert_id}")
                            pbar.set_description(f"[2/2] Ошибка обработки | ID: {cert_id}")
                            pbar.update(1)
                            
                        # Увеличиваем счетчик попыток и продолжаем цикл
                        retry_count += 1
                        if retry_count < max_retries:
                            logger.info(f"Повторная попытка {retry_count}/{max_retries} для сертификата {cert_id}")
                            time.sleep(0.5)  # Небольшая пауза перед повторной попыткой
                        else:
                            # Исчерпаны все попытки
                            logger.error(f"Исчерпаны все попытки для сертификата {cert_id}")
                            return
                else:
                    with self.lock:
                        logger.warning(f"Не удалось получить детали для сертификата {cert_id}")
                        pbar.set_description(f"[2/2] Нет данных | ID: {cert_id}")
                        pbar.update(1)
                        
                    # Увеличиваем счетчик попыток и продолжаем цикл
                    retry_count += 1
                    if retry_count < max_retries:
                        logger.info(f"Повторная попытка {retry_count}/{max_retries} для сертификата {cert_id}")
                        time.sleep(0.5)  # Небольшая пауза перед повторной попыткой
                    else:
                        # Исчерпаны все попытки
                        logger.error(f"Исчерпаны все попытки для сертификата {cert_id}")
                        return
                        
            except Exception as e:
                with self.lock:
                    logger.error(f"Ошибка при обработке сертификата {cert_id}: {e}")
                    pbar.set_description(f"[2/2] Ошибка | ID: {cert_id}")
                    pbar.update(1)
                return
                
        # Небольшая пауза между запросами для снижения нагрузки на API
        time.sleep(max(0.01, self.adaptive_delay / 4))

    def parse_data_for_date_range(self, start_date, end_date, per_page=100):
        start_date_str = self.format_date(start_date)
        end_date_str = self.format_date(end_date)
        
        # Шаг 1: Получаем первую страницу для определения общего количества 
        logger.info(f"Начинаем сбор данных за период {start_date_str} - {end_date_str}")
        data = self.get_certifications_by_date_range(start_date, end_date, page=1, per_page=per_page)
        
        if not data:
            logger.error(f"Не удалось получить данные за период {start_date_str} - {end_date_str}")
            return []
            
        if not data.get('items'):
            logger.warning(f"За период {start_date_str} - {end_date_str} нет данных")
            return []
            
        total_count = data.get('count', 0)
        total_pages = data.get('pages', 0)
        
        if total_count == 0 or total_pages == 0:
            logger.warning(f"За период {start_date_str} - {end_date_str} нет данных (count={total_count}, pages={total_pages})")
            return []
            
        logger.info(f"Всего найдено {total_count} записей на {total_pages} страницах")
        
        # Шаг 2: Собираем ID всех сертификатов с каждой страницы
        all_cert_ids = {}
        all_cert_ids[1] = [item.get('certdecltr_id') for item in data.get('items', [])]
        
        # Задаем количество потоков для загрузки списка
        self.current_threads = min(8, max(4, self.max_threads // 5))
        
        # Собираем ID с остальных страниц, если их больше одной
        if total_pages > 1:
            result_dict = {}
            with tqdm(total=total_pages - 1, desc=f"[1/2] Сбор списка | Потоки: {self.current_threads}",
                      bar_format='{desc} | {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]',
                      position=0, leave=True, colour='green') as pbar:
                
                # Разбиваем страницы на батчи для последовательной обработки
                pages = list(range(2, total_pages + 1))
                batch_size = self.current_threads
                page_batches = [pages[i:i+batch_size] for i in range(0, len(pages), batch_size)]
                
                # Обрабатываем каждый батч страниц параллельно
                for batch in page_batches:
                    with ThreadPoolExecutor(max_workers=self.current_threads) as executor:
                        futures = [executor.submit(
                            self.fetch_page_worker, 
                            page, 
                            start_date, 
                            end_date, 
                            per_page, 
                            result_dict, 
                            pbar
                        ) for page in batch]
                        
                        # Ждем завершения всех футуров
                        for future in as_completed(futures):
                            try:
                                future.result()
                            except Exception as e:
                                logger.error(f"Ошибка при сборе списка: {e}")
                    
                    # Небольшая пауза между батчами, чтобы не перегружать API
                    if batch != page_batches[-1]:
                        time.sleep(0.3)
            
            # Добавляем полученные ID к общему списку
            for page, ids in result_dict.items():
                if ids:  # Проверяем, что список не пустой
                    all_cert_ids[page] = ids
                    
        # Объединяем все ID в один список
        cert_ids = []
        for page in sorted(all_cert_ids.keys()):
            if all_cert_ids[page]:  # Проверяем, что список не пустой
                cert_ids.extend(all_cert_ids[page])
        
        # Удаляем дубликаты и None значения
        cert_ids = [cert_id for cert_id in cert_ids if cert_id is not None]
        cert_ids = list(dict.fromkeys(cert_ids))  # Удаляем дубликаты, сохраняя порядок
        
        total_certs = len(cert_ids)
        logger.info(f"Получено {total_certs} уникальных ID сертификатов/деклараций")
        
        if total_certs == 0:
            logger.warning("Нет данных для обработки")
            return []
            
        # Шаг 3: Получаем детальную информацию о каждом сертификате
        result_list = []
        with tqdm(total=total_certs, desc=f"[2/2] Детали | Потоки: {self.current_threads}", 
                  bar_format='{desc} | {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]',
                  position=0, leave=True, colour='blue') as pbar:
            
            # Разбиваем ID на батчи для последовательной обработки
            batch_size = self.current_threads * 2
            id_batches = [cert_ids[i:i+batch_size] for i in range(0, len(cert_ids), batch_size)]
            
            # Обрабатываем каждый батч ID параллельно
            for batch in id_batches:
                with ThreadPoolExecutor(max_workers=self.current_threads) as executor:
                    futures = []
                    for cert_id in batch:
                        future = executor.submit(
                            self.fetch_cert_details_worker, 
                            cert_id, 
                            result_list, 
                            pbar
                        )
                        futures.append(future)
                        
                    # Ждем завершения всех футуров
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception as e:
                            logger.error(f"Ошибка при получении деталей: {e}")
                
                # Небольшая пауза между батчами
                if batch != id_batches[-1]:
                    delay = 0.3
                    pbar.set_description(f"[2/2] Пауза {delay*1000:.0f}мс | Обработано: {pbar.n}/{total_certs}")
                    time.sleep(delay)

        # Проверяем полноту собранных данных
        if len(result_list) < total_certs:
            missing = total_certs - len(result_list)
            logger.warning(f"Внимание: получено {len(result_list)} записей из {total_certs} (не получено {missing})")
        else:
            logger.info(f"Успешно получены все {len(result_list)} записей с детальной информацией")
            
        return result_list

    def process_cert_data(self, cert_data):
        result = {}
        if not cert_data or not isinstance(cert_data, dict):
            logger.error("Получены некорректные данные для обработки")
            result['Регистрационный номер'] = "Ошибка получения данных"
            return result
        
        # Логируем структуру данных для отладки
        doc_id = cert_data.get('DocId', '') or cert_data.get('certdecltr_ConformityDocDetails', {}).get('DocId', '')
        logger.info(f"Обработка данных для сертификата {doc_id}")
        
        cert_details = cert_data.get('certdecltr_ConformityDocDetails', {})
        
        # Сведения о документе
        result['Регистрационный номер'] = doc_id
        result['Дата начала действия'] = cert_details.get('DocStartDate', '')
        result['Дата окончания действия'] = cert_details.get('DocValidityDate', '')
        
        # Статус действия сертификата
        doc_status = cert_details.get('DocStatusDetails', {})
        status_code = doc_status.get('DocStatusCode', '')
        status_mapping = {
            '01': 'Действует',
            '02': 'Прекращен',
            '03': 'Приостановлен',
            '04': 'Возобновлен',
            '05': 'Архивный',
        }
        result['Статус действия сертификата (декларации)'] = status_mapping.get(status_code, f'Неизвестный ({status_code})')
        
        # Вид документа об оценке соответствия
        doc_kind_code = cert_data.get('ConformityDocKindCode', '') or cert_details.get('ConformityDocKindCode', '')
        kind_mapping = {
            '01': 'Сертификат соответствия на продукцию',
            '02': 'Сертификат соответствия на услуги',
            '03': 'Сертификат соответствия на систему менеджмента',
            '05': 'Декларация о соответствии',
            '10': 'Сертификат соответствия ТР ТС/ЕАЭС',
            '15': 'Декларация о соответствии ТР ТС/ЕАЭС',
            '20': 'Сертификат соответствия ТР ТС/ЕАЭС',
        }
        result['Вид документа об оценке соответствия'] = kind_mapping.get(doc_kind_code, f'Неизвестный ({doc_kind_code})')
        
        # Номер технического регламента
        tech_regs = cert_details.get('TechnicalRegulationId', [])
        tech_regs = [tr for tr in tech_regs if tr] # Убираем None значения
        result['Номер технического регламента'] = ', '.join(tech_regs) if tech_regs else ''
        
        # Орган по сертификации
        conformity_authority = cert_details.get('ConformityAuthorityV2Details', {})
        result['Полное наименование органа по сертификации (из аттестата аккредитации)'] = conformity_authority.get('BusinessEntityName', '')
        
        # Данные заявителя
        applicant = cert_details.get('ApplicantDetails', {})
        result['Заявитель'] = applicant.get('BusinessEntityName', '')
        result['Страна (Заявитель)'] = applicant.get('UnifiedCountryCode', '')
        result['Краткое наименование хозяйствующего субъекта (Заявитель)'] = applicant.get('BusinessEntityBriefName', '') or applicant.get('BusinessEntityName', '')
        result['Идентификатор хозяйствующего субъекта (Заявитель)'] = applicant.get('BusinessEntityId', '')
        
        # Извлечение адреса заявителя из вложенной структуры
        subject_address = None
        if 'SubjectAddressDetails' in applicant and applicant['SubjectAddressDetails']:
            if isinstance(applicant['SubjectAddressDetails'], list):
                subject_address = applicant['SubjectAddressDetails'][0]
            else:
                subject_address = applicant['SubjectAddressDetails']
        
        if subject_address:
            address_parts = []
            country = subject_address.get('UnifiedCountryCode', '')
            if country:
                address_parts.append(f"Страна: {country}")
            
            postal = subject_address.get('PostCode', '') or subject_address.get('PostalCode', '')
            if postal:
                address_parts.append(f"Индекс: {postal}")
                
            region = subject_address.get('RegionName', '')
            if region:
                address_parts.append(f"Регион: {region}")
                
            city = subject_address.get('CityName', '')
            if city:
                address_parts.append(f"Город: {city}")
                
            street = subject_address.get('StreetName', '')
            if street:
                address_parts.append(f"Улица: {street}")
                
            house = subject_address.get('HouseNumber', '') or subject_address.get('BuildingNumberId', '')
            if house:
                address_parts.append(f"Дом: {house}")
                
            building = subject_address.get('BuildingNumber', '')
            if building:
                address_parts.append(f"Строение: {building}")
                
            room = subject_address.get('RoomNumber', '') or subject_address.get('RoomNumberId', '')
            if room:
                address_parts.append(f"Помещение: {room}")
                
            result['Адрес заявителя'] = ', '.join(address_parts)
        else:
            result['Адрес заявителя'] = ''
        
        # Извлечение контактных данных заявителя
        if 'CommunicationDetails' in applicant and applicant['CommunicationDetails']:
            contact_parts = []
            for comm in applicant['CommunicationDetails']:
                channel_code = comm.get('CommunicationChannelCode', '')
                channel_ids = comm.get('CommunicationChannelId', [])
                
                if channel_ids and channel_code:
                    if channel_code == 'TE':
                        prefix = "Тел.: "
                    elif channel_code == 'FX':
                        prefix = "Факс: "
                    elif channel_code == 'EM':
                        prefix = "Email: "
                    else:
                        prefix = f"{channel_code}: "
                    
                    for channel_id in channel_ids:
                        if channel_id:  # Проверяем, что значение не пустое
                            contact_parts.append(f"{prefix}{channel_id}")
            
            result['Контактный реквизит заявителя'] = '; '.join(contact_parts)
        else:
            result['Контактный реквизит заявителя'] = ''
        
        # Данные изготовителя
        manufacturer = None
        # Сначала проверяем в основных данных сертификата
        manufacturer_list = cert_details.get('ManufacturerDetails', [])
        if manufacturer_list and len(manufacturer_list) > 0:
            manufacturer = manufacturer_list[0]
        
        # Если не найдено, проверяем внутри TechnicalRegulationObjectDetails
        if not manufacturer and 'TechnicalRegulationObjectDetails' in cert_details:
            tech_obj = cert_details['TechnicalRegulationObjectDetails']
            if 'ManufacturerDetails' in tech_obj and tech_obj['ManufacturerDetails']:
                manufacturer = tech_obj['ManufacturerDetails'][0]
        
        if manufacturer:
            result['Изготовитель'] = manufacturer.get('BusinessEntityName', '')
            result['Страна (Изготовитель)'] = manufacturer.get('UnifiedCountryCode', '')
            result['Краткое наименование хозяйствующего субъекта (Изготовитель)'] = manufacturer.get('BusinessEntityBriefName', '') or manufacturer.get('BusinessEntityName', '')
            
            # Извлечение адреса изготовителя
            manuf_address = None
            if 'SubjectAddressDetails' in manufacturer and manufacturer['SubjectAddressDetails']:
                if isinstance(manufacturer['SubjectAddressDetails'], list):
                    manuf_address = manufacturer['SubjectAddressDetails'][0]
                else:
                    manuf_address = manufacturer['SubjectAddressDetails']
            elif 'AddressV4Details' in manufacturer and manufacturer['AddressV4Details']:
                manuf_address = manufacturer['AddressV4Details'][0]
            
            if manuf_address:
                address_parts = []
                country = manuf_address.get('UnifiedCountryCode', '')
                if country:
                    address_parts.append(f"Страна: {country}")
                
                postal = manuf_address.get('PostCode', '') or manuf_address.get('PostalCode', '')
                if postal:
                    address_parts.append(f"Индекс: {postal}")
                    
                region = manuf_address.get('RegionName', '')
                if region and region != "-":
                    address_parts.append(f"Регион: {region}")
                    
                city = manuf_address.get('CityName', '')
                if city:
                    address_parts.append(f"Город: {city}")
                    
                street = manuf_address.get('StreetName', '')
                if street:
                    address_parts.append(f"Улица: {street}")
                    
                house = manuf_address.get('HouseNumber', '') or manuf_address.get('BuildingNumberId', '')
                if house:
                    address_parts.append(f"Дом: {house}")
                    
                building = manuf_address.get('BuildingNumber', '')
                if building:
                    address_parts.append(f"Строение: {building}")
                    
                room = manuf_address.get('RoomNumber', '') or manuf_address.get('RoomNumberId', '')
                if room:
                    address_parts.append(f"Помещение: {room}")
                    
                result['Адрес изготовителя'] = ', '.join(address_parts)
            else:
                result['Адрес изготовителя'] = ''
            
            # Извлечение контактных данных изготовителя
            if 'CommunicationDetails' in manufacturer and manufacturer['CommunicationDetails']:
                contact_parts = []
                for comm in manufacturer['CommunicationDetails']:
                    channel_code = comm.get('CommunicationChannelCode', '')
                    channel_ids = comm.get('CommunicationChannelId', [])
                    
                    if channel_ids and channel_code and channel_code != 'null':
                        if channel_code == 'TE':
                            prefix = "Тел.: "
                        elif channel_code == 'FX':
                            prefix = "Факс: "
                        elif channel_code == 'EM':
                            prefix = "Email: "
                        else:
                            prefix = f"{channel_code}: "
                        
                        for channel_id in channel_ids:
                            if channel_id:  # Проверяем, что значение не пустое
                                contact_parts.append(f"{prefix}{channel_id}")
                
                result['Контактный реквизит изготовителя'] = '; '.join(contact_parts)
            else:
                result['Контактный реквизит изготовителя'] = ''
        else:
            # Если данных об изготовителе нет, заполняем пустыми строками
            result['Изготовитель'] = ''
            result['Страна (Изготовитель)'] = ''
            result['Краткое наименование хозяйствующего субъекта (Изготовитель)'] = ''
            result['Адрес изготовителя'] = ''
            result['Контактный реквизит изготовителя'] = ''
        
        # Данные об объекте технического регулирования
        tech_reg_obj = cert_details.get('TechnicalRegulationObjectDetails', {})
        if tech_reg_obj:
            object_name = tech_reg_obj.get('TechnicalRegulationObjectName', '')
            kind_name = tech_reg_obj.get('TechnicalRegulationObjectKindName', '')
            
            if object_name:
                result['Объект технического регулирования'] = object_name
            elif kind_name:
                result['Объект технического регулирования'] = kind_name
            else:
                result['Объект технического регулирования'] = cert_details.get('TechnicalRegulationObject', '') or cert_details.get('CertificationObjectName', '')
        else:
            result['Объект технического регулирования'] = cert_details.get('TechnicalRegulationObject', '') or cert_details.get('CertificationObjectName', '')
        
        # Данные о продукте
        product_details = None
        
        # Сначала ищем в основных данных сертификата
        if 'ProductDetails' in cert_details and cert_details['ProductDetails']:
            product_details = cert_details['ProductDetails']
        
        # Затем проверяем внутри TechnicalRegulationObjectDetails
        if not product_details and 'TechnicalRegulationObjectDetails' in cert_details:
            tech_obj = cert_details['TechnicalRegulationObjectDetails']
            if 'ProductDetails' in tech_obj and tech_obj['ProductDetails']:
                product_details = tech_obj['ProductDetails']
        
        if product_details and len(product_details) > 0:
            product = product_details[0]
            
            # Название продукта
            product_name = product.get('ProductName', '')
            if not product_name and 'ProductInstanceDetails' in product:
                instances = product.get('ProductInstanceDetails', [])
                if instances and len(instances) > 0:
                    product_name = instances[0].get('ProductName', '')
                    
            result['Наименование объекта оценки соответствия'] = product_name
            
            # Коды товара
            commodity_codes = []
            if 'CommodityCode' in product and product['CommodityCode']:
                commodity_codes.extend([c for c in product['CommodityCode'] if c])
            
            if 'CommodityCodeList' in product and product['CommodityCodeList']:
                commodity_codes.extend([c for c in product['CommodityCodeList'] if c])
                
            result['Код товара по ТН ВЭД ЕАЭС'] = ', '.join(commodity_codes) if commodity_codes else ''
        else:
            # Пробуем получить данные из других полей
            result['Наименование объекта оценки соответствия'] = cert_details.get('CertificationObjectName', '')
            result['Код товара по ТН ВЭД ЕАЭС'] = ''
        
        return result

    def save_to_excel(self, data, filename):
        if not data:
            logger.error("Нет данных для сохранения")
            return None
        has_only_reg_number = all(len(item.keys()) <= 1 and 'Регистрационный номер' in item for item in data)
        if has_only_reg_number:
            logger.warning("ВНИМАНИЕ: Данные содержат только регистрационные номера без дополнительной информации!")
        os.makedirs(self.output_folder, exist_ok=True)
        filepath = os.path.join(self.output_folder, filename)
        try:
            num_rows = len(data)
            num_cols = len(data[0].keys()) if data else 0
            logger.info(f"Сохранение {num_rows} строк с {num_cols} колонками в {filepath}")
            df = pd.DataFrame(data)
            
            # Сохраняем в Excel с использованием openpyxl для дополнительной настройки
            with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
                df.to_excel(writer, index=False, sheet_name='Данные')
                
                # Получаем лист и устанавливаем ширину колонок и параметры фиксации
                worksheet = writer.sheets['Данные']
                for idx, col in enumerate(df.columns):
                    # Устанавливаем ширину 500 пикселей (~ 71 единица в Excel)
                    worksheet.column_dimensions[chr(65 + idx)].width = 71
                
                # Фиксируем заголовки при прокрутке
                worksheet.freeze_panes = 'A2'
            
            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                print(f"\nФайл сохранен: {filepath}")
                print(f"Строк: {num_rows}, Колонок: {num_cols}, Размер: {os.path.getsize(filepath)/1024:.1f} KB")
                logger.info(f"Файл сохранен успешно: {filepath} ({os.path.getsize(filepath)/1024:.1f} KB)")
                return filepath
            else:
                logger.error(f"Ошибка: файл {filepath} не создан или пуст")
                return None
        except Exception as e:
            logger.error(f"Ошибка при сохранении данных в Excel: {e}")
            return None

    def adjust_threads_and_retries(self, success=True):
        with self.lock:
            if success:
                self.success_counter += 1
                self.error_counter = max(0, self.error_counter - 1)
                self.consecutive_fails = 0
                self.consecutive_success += 1
                if self.consecutive_success > 5:
                    self.adaptive_delay = max(0.01, self.adaptive_delay * 0.9)
                if self.consecutive_success >= 10 and self.current_threads < self.max_threads:
                    self.current_threads = min(self.max_threads, self.current_threads + 1)
                    self.success_counter = 0
                    logger.info(f"Увеличено число потоков до {self.current_threads}")
            else:
                self.error_counter += 1
                self.success_counter = max(0, self.success_counter - 1)
                self.consecutive_success = 0
                self.consecutive_fails += 1
                if self.consecutive_fails > 2:
                    self.adaptive_delay = min(self.delay_range[1] * 1.2, self.adaptive_delay * 1.1)
                if self.error_counter >= self.error_threshold:
                    if self.current_threads > 2:
                        decrement = 2 if self.consecutive_fails >= 2 else 1
                        self.current_threads = max(2, self.current_threads - decrement)
                        logger.info(f"Уменьшено число потоков до {self.current_threads}")
                    self.current_retries = min(self.max_retries + 2, self.current_retries + 1)
                    self.error_counter = 0
                    logger.info(f"Увеличено число повторов до {self.current_retries}")

def interactive_date_input():
    try:
        while True:
            print("\nВведите даты в формате ДД.ММ.ГГГГ")
            start_date_str = input("Начальная дата: ")
            end_date_str = input("Конечная дата: ")
            try:
                start_date = datetime.strptime(start_date_str, "%d.%m.%Y")
                end_date = datetime.strptime(end_date_str, "%d.%m.%Y")
                if end_date < start_date:
                    print("Ошибка: дата окончания не может быть раньше даты начала")
                    return None, None
                return start_date, end_date
            except ValueError as e:
                print(f"Ошибка в формате даты: {e}")
                print("Убедитесь, что даты указаны в формате ДД.ММ.ГГГГ")
    except Exception as e:
        print(f"Ошибка при вводе дат: {e}")
        return None, None

def main():
    belgiss = BelgissParser()
    start_date, end_date = interactive_date_input()
    if start_date and end_date:
        data = belgiss.parse_data_for_date_range(start_date, end_date)
        if data:
            filename = f"certifications_{start_date.strftime('%Y%m%d')}-{end_date.strftime('%Y%m%d')}.xlsx"
            filepath = belgiss.save_to_excel(data, filename)
            if filepath:
                print("\nПарсинг и сохранение завершены успешно!")
                input("\nНажмите Enter для завершения работы программы...")

if __name__ == "__main__":
    main()