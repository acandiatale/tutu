package com.example.greeting;

import static org.junit.Assert.assertEquals;

import java.util.Calendar;

import org.junit.Test;

public class GreetingServiceTest {

    private final GreetingService service = new GreetingService();

    @Test
    public void afternoonUsesDefaultMessage() {
        Calendar cal = Calendar.getInstance();
        cal.set(Calendar.HOUR_OF_DAY, 14);
        assertEquals("È«±æµ¿´Ô, ¾È³çÇÏ¼¼¿ä!", service.greet("È«±æµ¿", cal).render());
    }

    @Test
    public void blankNameFallsBackToCustomer() {
        Calendar cal = Calendar.getInstance();
        cal.set(Calendar.HOUR_OF_DAY, 9);
        assertEquals("°í°´´Ô, ÁÁÀº ¾ÆÄ§ÀÔ´Ï´Ù!", service.greet(" ", cal).render());
    }
}
