package com.hsbc.hase.ingress.listener;

import org.springframework.jms.annotation.JmsListener;
import org.springframework.stereotype.Component;

@Component
public class IngressListener {
    @JmsListener(destination = "${ingress.sample.queue}")
    public void onMessage(String payload) {
    }
}
