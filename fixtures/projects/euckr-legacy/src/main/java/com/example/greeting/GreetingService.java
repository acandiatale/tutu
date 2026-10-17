package com.example.greeting;

import java.util.Calendar;

import org.apache.commons.lang3.StringUtils;

/** 시간대별 인사말 생성 서비스 (오전/오후/저녁). */
public class GreetingService {

    public Greeting greet(String name, Calendar now) {
        String who = StringUtils.defaultIfBlank(name, "고객");
        int hour = now.get(Calendar.HOUR_OF_DAY);
        String message;
        if (hour < 12) {
            message = "좋은 아침입니다";
        } else if (hour < 18) {
            message = Greeting.DEFAULT_MESSAGE;
        } else {
            message = "좋은 저녁 되세요";
        }
        return new Greeting(who, message);
    }
}
