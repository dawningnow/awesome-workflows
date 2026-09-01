import requests
import json
import os
import time
from bs4 import BeautifulSoup
import datetime
import smtplib
from email.utils import formataddr
from pathlib import Path
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def get_weather(my_city):
    urls = ["http://www.weather.com.cn/textFC/hb.shtml",
            "http://www.weather.com.cn/textFC/db.shtml",
            "http://www.weather.com.cn/textFC/hd.shtml",
            "http://www.weather.com.cn/textFC/hz.shtml",
            "http://www.weather.com.cn/textFC/hn.shtml",
            "http://www.weather.com.cn/textFC/xb.shtml",
            "http://www.weather.com.cn/textFC/xn.shtml"
            ]
    for url in urls:
        resp = requests.get(url)
        text = resp.content.decode("utf-8")
        soup = BeautifulSoup(text, 'lxml')
        div_conMidtab = soup.find("div", class_="conMidtab")
        tables = div_conMidtab.find_all("table")
        for table in tables:
            trs = table.find_all("tr")[2:]
            for  tr in trs:
                tds = tr.find_all("td")
                # 这里倒着数，因为每个省会的td结构跟其他不一样
                city_td = tds[-8]
                this_city = list(city_td.stripped_strings)[0]
                if this_city == my_city:

                    high_temp_td = tds[-5]
                    low_temp_td = tds[-2]
                    weather_type_day_td = tds[-7]
                    weather_type_night_td = tds[-4]
                    wind_td_day = tds[-6]
                    wind_td_day_night = tds[-3]

                    high_temp = list(high_temp_td.stripped_strings)[0]
                    low_temp = list(low_temp_td.stripped_strings)[0]
                    weather_typ_day = list(weather_type_day_td.stripped_strings)[0]
                    weather_type_night = list(weather_type_night_td.stripped_strings)[0]

                    wind_day = list(wind_td_day.stripped_strings)[0] + list(wind_td_day.stripped_strings)[1]
                    wind_night = list(wind_td_day_night.stripped_strings)[0] + list(wind_td_day_night.stripped_strings)[1]

                    # 如果没有白天的数据就使用夜间的
                    temp = f"{low_temp}°C ~ {high_temp}°C" if high_temp != "-" else f"{low_temp}°C"
                    weather_typ = weather_typ_day if weather_typ_day != "-" else weather_type_night
                    wind = f"{wind_day}" if wind_day != "--" else f"{wind_night}"
                    return this_city, weather_typ, temp, wind


def get_daily_love():
    url = "https://v1.hitokoto.cn/"
    res = requests.get(url)
    data = res.json()
    return data['hitokoto'] + '     --- ' + data['from']


def get_daily_image():
    url = "https://open.iciba.com/dsapi/"
    res = requests.get(url)
    all_dict = json.loads(res.text)
    image_url = all_dict["fenxiang_img"]
    response = requests.get(image_url, timeout=10)
    return response.content


def email_notice(daily_image, receiver, html_content):
    sender_email = os.getenv("SENDER_EMAIL", "")
    auth_code = os.getenv("AUTH_CODE", "")
    receiver_email = os.getenv(receiver, "")

    # 创建邮件容器，支持内联资源（如图片）
    message =  MIMEMultipart('related')  

    message['From'] = formataddr(('dawn', sender_email))
    message['To'] = formataddr(('*', receiver_email))
    message['Subject'] = '早安！' 

    # 插入html至邮件中
    message.attach(MIMEText(html_content, 'html', 'utf-8'))

    # 添加GIF
    with open('weather/bell.gif', 'rb') as f:
        img_data = f.read()
        img_part = MIMEImage(img_data, 'gif')
        img_part.add_header('Content-ID', '<bell_image>')
        img_part.add_header('Content-Disposition', 'inline', filename="bell_image.gif")
        message.attach(img_part)

    # 添加图片
    image = MIMEImage(daily_image, _subtype="png")
    image.add_header("Content-ID", "<daily_image>")
    image.add_header("Content-Disposition", "inline",filename="daily_image.png")
    message.attach(image)

    try:
        server = smtplib.SMTP_SSL('smtp.qq.com', 465)
        server.login(sender_email, auth_code)
        server.sendmail(sender_email, [receiver_email], message.as_string())
        server.quit()
        print("邮件发送成功！")
    except Exception as e:
        print(f"邮件发送失败 !")


def weather_report(sweetnothings, daily_image,receiver, this_city):
    weather = get_weather(this_city)
    weather_icons = {
        '晴': '☀️',
        '多云': '⛅',
        '阴': '☁️',
        '小雨': '🌦️',
        '中雨': '🌧️',
        '大雨': '🌧️',
        '雷阵雨': '⛈️',
        '小雪': '🌨️',
        '中雪': '❄️',
        '大雪': '❄️',
        '雾': '🌫️',
        '霾': '🌫️',
        '风': '💨',
        '台风': '🌀',
        '沙尘暴': '🏜️',
        '晴间多云': '🌤️',
        '多云转晴': '⛅',
        '阴天': '☁️',
        '阵雨': '🌦️',
        '暴雨': '🌊',
        '雨夹雪': '🌨️',
        '冰雹': '🧊',
        '霜冻': '🥶',
        '高温': '🥵',
        '低温': '🥶',
        '暴雪': '❄️',
        '雾霾': '🌫️',
        '扬沙': '🏜️',
        '浮尘': '🏜️',
        '强风': '💨',
        '大风': '💨',
    }
    weather_icon = weather_icons.get(weather[1], ' ')

    weekdays = ['一', '二', '三', '四', '五', '六', '日']
    weather_data = {
        'date': datetime.date.today().strftime('%Y年%m月%d日'),
        'week':weekdays[datetime.datetime.now().weekday()],
        'location': weather[0],
        'weather_type': weather[1],
        'weather_icon': weather_icon,
        'temp': weather[2],
        'wind': weather[3],
        'SweetNothings': sweetnothings,
        'update': datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
    }

    html = Path("weather/weather_template.html").read_text(encoding="utf-8")
    html_content = html.format(**weather_data)
    email_notice(daily_image, receiver, html_content)


if __name__ == '__main__':
    sweetnothings = get_daily_love()
    daily_image = get_daily_image()
    weather_report(sweetnothings, daily_image, "TEST_EMAIL", "武汉")
    # time.sleep(5)
    # weather_report(sweetnothings, daily_image,"CH_EMAIL", "长春")