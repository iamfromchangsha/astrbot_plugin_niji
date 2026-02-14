import requests
import re
import json
from datetime import datetime, timezone, timedelta








def get_current_date_str():

    # 1. 设置时区为北京时间（UTC+8），避免使用默认UTC时区导致日期偏差
    beijing_tz = timezone(timedelta(hours=8))
    # 2. 获取当前北京时间
    current_time = datetime.now(beijing_tz)
    # 3. 按照指定格式格式化日期
    date_str = current_time.strftime("%Y-%m-%d")
    
    return date_str

def compare_date_str(date_str1, date_str2, format_str="%Y-%m-%d"):

    try:
        # 解析字符串为 date 对象（只保留日期，无时间）
        date1 = datetime.strptime(date_str1, format_str).date()
        date2 = datetime.strptime(date_str2, format_str).date()
        
        # 比较大小
        if date1 > date2:
            return 1
        elif date1 < date2:
            return -1
        else:
            return 0
    except ValueError as e:
        print(f"日期格式错误：{e}")
        return None












def post_diary(token,title,content,id):
    url = "https://nijiweb.cn/api/"
    headers = {
        "Cookie":"token="+token,
        "content-type": "application/x-www-form-urlencoded",
        "user-agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0"
    }
    data={
        "function":"writeDiary",
        "dateText":"2026-02-14",
        "titleText":title,
        "contentText":content,
        "id":id
    }
    response = requests.post(url, headers=headers, data=data)
    print(response.text)

def get_diary(token,ownerid,diaryid,userid):
    url = "https://nijiweb.cn/api/"
    headers = {
        "Cookie":"token="+token,
        "content-type": "application/x-www-form-urlencoded",
        "user-agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0"
    }
    data={
        "function":"getDiary",
        "ownerId":ownerid,
        "diaryId":diaryid,
        "userId":userid
    }
    response = requests.post(url, headers=headers, data=data)
    return response.json()
def login(username,password):
    url = "https://nijiweb.cn/api/login/"
    headers = {
        "content-type": "application/x-www-form-urlencoded",
        "user-agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0"    
    }
    data={
        "email":username,
        "password":password
    }
    response = requests.post(url, headers=headers, data=data)
    token = response.cookies.get('token')
    print(token)
    return token

def get_userid_and_diarycard(token):
    url = "https://nijiweb.cn/"
    headers = {
        "Cookie": "token=" + token,
        "content-type": "application/x-www-form-urlencoded",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36 Edg/144.0.0.0"
    }
    
    # 初始化返回值
    user_id = None
    diary_cards_data = []  # 修改为列表，存储所有日记卡片数据
    
    try:
        # 发送请求获取页面内容（只请求一次，避免重复请求）
        response = requests.get(url, headers=headers)
        response.raise_for_status()  # 抛出HTTP错误
        html_content = response.text
        
        # 1. 提取 setUserId 中的用户ID
        userid_pattern = r"setUserId\((\d+)\);"
        userid_match = re.search(userid_pattern, html_content)
        if userid_match:
            user_id = userid_match.group(1)
        
        # 2. 提取所有 addDiaryCard 中的完整JSON数据
        # 使用 findall 找到所有匹配项，而不仅仅是第一个
        diary_pattern = r"addDiaryCard\((\{[^}]*\})\);"
        diary_matches = re.findall(diary_pattern, html_content)
        
        # 遍历所有匹配项并解析为JSON对象
        for json_str in diary_matches:
            # 清理格式，替换单引号为双引号（JSON标准要求双引号）
            clean_json_str = json_str.replace("'", '"')
            try:
                # 解析为Python字典并添加到列表
                diary_data = json.loads(clean_json_str)
                diary_cards_data.append(diary_data)
            except json.JSONDecodeError as e:
                print(f"单个JSON解析出错: {e}, 原始内容: {json_str}")
                continue  # 继续处理下一个匹配项
        
        # 返回用户ID和所有日记卡片数据（元组形式）
        return user_id, diary_cards_data
            
    except requests.exceptions.RequestException as e:
        print(f"请求出错: {e}")
        return None, []
    except json.JSONDecodeError as e:
        print(f"JSON解析出错: {e}")
        return user_id, []  # 即使JSON解析失败，也返回已获取的user_id和空列表
    except Exception as e:
        print(f"解析出错: {e}")
        return None, []


   

curent_time = get_current_date_str()
token=login("2212831947@qq.com","89937.7374")
user_id, diary_card_data = get_userid_and_diarycard(token)
check = False
createdate = None
print(user_id)
for diary_card in diary_card_data:
    if str(diary_card.get("cardUserID")) == str(user_id) and str(compare_date_str(curent_time,diary_card.get("createdDate"))) == '0':
        check = True
        diaryid = diary_card.get("cardDiaryId")
        break
print(check)
if check:
    diary_content=get_diary(token,user_id,diaryid,user_id).get("content")
    print(diary_content)
    
    


        


# print(f"用户ID: {user_id}")
# print(f"日记卡片数据: {diary_card_data}")


# print(f"日记卡片数据: {diary_card_data.get('cardDiaryId')}")
# diaryid = diary_card_data.get('cardDiaryId')# 这个时间是目前列表中第一个日记创建时间
# # get_diary(token,user_id,"1",user_id)
# diary_content = get_diary(token,user_id,diaryid,user_id)# 



# print(diary_content)
# content = diary_content.get("content")
# print(content)
# print(curent_time)
