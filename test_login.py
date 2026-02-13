"""
Playwright 测试用例 - 登录功能测试

测试步骤:
1. 访问 http://115.190.27.121/session?view
2. 输入用户名 admin
3. 输入密码 1q2w#E$R
4. 点击Login
5. 检查浏览器当前url应该为 http://115.190.27.121/dashboard?view
"""

import re
from playwright.sync_api import Page, expect


def test_login_success(page: Page):
    """测试登录成功场景"""
    
    # Step 1: 访问登录页面
    page.goto("http://115.190.27.121/session?view")
    page.wait_for_timeout(1000)  # 等待1秒，方便观察
    
    # Step 2: 输入用户名 - 使用 name 属性定位
    username_input = page.locator('input[name="username"]')
    username_input.fill("admin")
    page.wait_for_timeout(500)  # 等待0.5秒
    
    # Step 3: 输入密码 - 使用 name 属性定位
    password_input = page.locator('input[name="password"]')
    password_input.fill("1q2w#E$R")
    page.wait_for_timeout(500)  # 等待0.5秒
    
    # Step 4: 点击登录按钮 - 支持中英文
    # 页面会根据浏览器语言显示 "Login" 或 "登录"
    login_button = page.locator('button:has-text("Login"), button:has-text("登录")')
    login_button.click()
    page.wait_for_timeout(2000)  # 等待2秒，观察跳转
    
    # Step 5: 等待并验证跳转后的URL
    expect(page).to_have_url("http://115.190.27.121/dashboard?view", timeout=10000)
